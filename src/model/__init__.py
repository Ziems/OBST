import typing

import mesh_tensorflow as mtf
import tensorflow as tf

from .activation import activate
from .backend import linear, linear_from_features, linear_to_features
from .basic import activated_linear
from .embedding import embed
from .frontend import block_part_fn
from .momentumnet import MomentumOperation
from .normalization import norm
from .revnet import RevGradOp
from ..dataclass import BlockArgs, BlockConfig, ModelParameter
from ..mtf_wrapper import (add_n, cast, constant_scalar, dropout, einsum, one_hot, ones, reciprocal, reduce_logsumexp,
                           reduce_mean, reduce_sum, sigmoid, sign, zeros_like, mod, floordiv, reshape, equal, argmax,
                           softmax_cross_entropy_with_logits, recompute_grad, add, negative, divide)
from ..utils_mtf import concat, utils_slice, weighted_add, anonymize, anonymize_shape
from ..utils_core import scoped

ATTENTION_DIM = typing.NamedTuple("AttentionDim", (('index', int), ('dim', mtf.Dimension)))

tf1 = tf.compat.v1


def _default_ones(params: ModelParameter, inp: typing.Optional[mtf.Tensor]) -> mtf.Tensor:
    if inp is None:
        return ones(params.mesh, [], params.variable_dtype.activation_dtype)
    return cast(inp, params.variable_dtype.activation_dtype)


def _input(params: ModelParameter,
           vid: typing.Optional[mtf.Tensor],
           cat_msk_src: typing.Optional[mtf.Tensor],
           txt_src: typing.Optional[mtf.Tensor],
           vid_msk_src: typing.Optional[mtf.Tensor],
           spatial_ctx: mtf.Dimension) -> typing.Tuple[mtf.Tensor, typing.Optional[mtf.Tensor]]:
    tgt = None
    if params.use_video:
        base_args = BlockArgs(params, vid, [''])

        vid = cast(vid, params.variable_dtype.activation_dtype)
        vid = dropout(vid, params.train, rate=params.input_dropout)

        if params.use_bit_fold_input_pipeline:
            vid = cast(vid, dtype=tf.int64)

            concat_list = []
            for unfold_idx in range(params.fold_count):
                var = mod(floordiv(vid, (2 ** params.bit_fold_value) ** unfold_idx),
                          (2 ** params.bit_fold_value))
                var = cast(var, dtype=tf.uint8)

                concat_list.append(var)

            vid = concat(concat_list, 'color_channels')

        if not params.use_discrete_video_loss:
            vid = divide(cast(vid, params.variable_dtype.activation_dtype), 255)
        context_dimension = vid.shape[1]
        input_features = vid.shape[-1:]
        tgt = utils_slice(vid, 1, context_dimension.size, context_dimension)
        src = utils_slice(vid, 0, context_dimension.size - 1, context_dimension)

        if params.use_discrete_video_loss:
            src = divide(cast(src, params.variable_dtype.activation_dtype), (params.color_quantization_value - 1))

            tgt = reshape(tgt, new_shape=mtf.Shape([params.batch_dim,
                                                    params.sequence_per_head_dim,
                                                    params.head_dim]
                                                   + tgt.shape[2:]))

        if params.empty_frame_embedding is not None:
            embed_args = base_args(params.empty_frame_embedding)
            src = weighted_add(src, embed(embed_args, vid.shape[2:]), vid_msk_src)
            src = weighted_add(src, embed(embed_args, vid.shape[2:]), cat_msk_src)

        src = linear_to_features(base_args(src), input_features)

        for config_idx, config in enumerate(params.input_block_config):
            src = block_part_fn(params, config, src, f'vid_inp{config_idx}')

    # Language embedding and initial feed forward.
    if params.use_language:
        base_args = BlockArgs(params, txt_src, [''])
        intermediate = params.intermediate[0]
        intermediate = mtf.Dimension(intermediate.name, int(intermediate.size * params.vocab_weight_factorization))
        txt_embd = embed(base_args(params.token_embedding), [params.vocab_dim, intermediate])
        txt = einsum([txt_embd, one_hot(txt_src, params.vocab_dim, dtype=params.variable_dtype.activation_dtype)],
                     reduced_dims=[params.vocab_dim])

        txt = dropout(txt, params.train, rate=params.input_dropout)

        txt = linear_to_features(base_args(txt), [params.token_patch_dim, intermediate])

        for config_idx, config in enumerate(params.input_block_config):
            txt = block_part_fn(params, config, txt, f'lang_inp{config_idx}')

    if params.use_video and params.use_language:
        return concat([src, txt], spatial_ctx), tgt
    if not params.use_video:
        return txt, tgt
    return src, tgt


def _body(params: ModelParameter, src: mtf.Tensor) -> mtf.Tensor:
    base_args = BlockArgs(params, src, [''])

    if params.use_initial_position_embedding:
        for dim in (src.shape - params.feature_dims).dims[1:]:
            src = add(src, embed(base_args(params.position_embedding), [dim] + params.feature_dims))

    if params.memory_reduction_strategy == "revnet":
        out = (src, zeros_like(src), src, zeros_like(src))

        def _layer_builder(block_input: typing.Tuple[mtf.Tensor, mtf.Tensor, mtf.Tensor, mtf.Tensor],
                           block_config: BlockConfig, index: int):
            return RevGradOp(params, block_config, *block_input, str(index)).outputs
    elif params.memory_reduction_strategy == 'checkpoint':
        out = src

        def _layer_builder(block_input: mtf.Tensor, block_config: BlockConfig, index: int):
            return recompute_grad(lambda x: block_part_fn(params, block_config, x, str(index)),
                                  [block_input])
    elif params.memory_reduction_strategy == 'momentum':
        out = (src, zeros_like(src), src, zeros_like(src))

        def _layer_builder(block_input: typing.Tuple[mtf.Tensor, mtf.Tensor, mtf.Tensor, mtf.Tensor],
                           block_config: BlockConfig, index: int):
            return MomentumOperation(params, block_config, *block_input, str(index)).outputs
    elif params.memory_reduction_strategy == 'none':
        out = src

        def _layer_builder(block_input: mtf.Tensor, block_config: BlockConfig, index: int):
            return block_part_fn(params, block_config, block_input, str(index))
    for i in range(params.n_blocks):
        for block_part in params.block_config:
            out = _layer_builder(out, block_part, i)

    if params.memory_reduction_strategy in ('revnet', 'momentum'):
        out = add(out[0], out[2])
    return out


def _output(params: ModelParameter, out: mtf.Tensor, spatial_ctx: mtf.Dimension
            ) -> typing.Tuple[typing.Optional[mtf.Tensor], typing.Optional[mtf.Tensor]]:
    base_args = BlockArgs(params, out, [''])
    token_out = frame_out = None

    if params.use_language:
        token_out = utils_slice(out, 0, params.language_token_patch, spatial_ctx)
        for config_idx, config in enumerate(params.output_block_config):
            token_out = block_part_fn(params, config, token_out, f'lang_out{config_idx}')
        new = [params.token_patch_dim, params.vocab_dim]
        old = params.feature_dims
        if params.split_vocab:
            old = anonymize_shape(old, params.head_dim)
            token_out = anonymize(token_out, params.head_dim)
        token_out = einsum([token_out, embed(base_args(params.output_embedding), old + new)],
                           output_shape=token_out.shape - old + new)

    if params.use_video:
        frame_out = utils_slice(out, params.language_token_patch * params.use_language, out.shape[2].size,
                                spatial_ctx)

        for config_idx, config in enumerate(params.output_block_config):
            frame_out = block_part_fn(params, config, frame_out, f'vid_out{config_idx}')

        if params.use_discrete_video_loss:

            features_dim = mtf.Dimension("features", frame_out.shape[-1].size * frame_out.shape[-2].size)
            frame_out = reshape(frame_out, frame_out.shape[:-2] + [features_dim])
            frame_out = reshape(frame_out,
                                [params.batch_dim, params.sequence_per_head_dim, params.head_dim]
                                + frame_out.shape[2:])

            frame_out = linear(base_args(frame_out), [features_dim],
                               [params.color_channel_dim, params.discrete_color_dim])

        else:
            frame_out = sigmoid(linear_from_features(base_args(frame_out), [params.color_channel_dim]))

    return frame_out, token_out


def _loss(params: ModelParameter, frame_out: typing.Optional[mtf.Tensor], token_out: typing.Optional[mtf.Tensor],
          txt_tgt: mtf.Tensor, loss_list: typing.List[mtf.Tensor], vid_msk_tgt: mtf.Tensor, cat_msk_tgt: mtf.Tensor,
          vid_tgt: mtf.Tensor) -> typing.Tuple[typing.List[mtf.Tensor], typing.Optional[mtf.Tensor],
                                               typing.Optional[mtf.Tensor], typing.Optional[mtf.Tensor]]:
    token_loss = accuracy = video_loss = None
    if params.use_language:
        token_loss = softmax_cross_entropy_with_logits(params, token_out, txt_tgt)
        loss_list.append(token_loss)
        if params.calc_accuracy:
            accuracy = divide(reduce_sum(cast(equal(argmax(token_out, params.vocab_dim), txt_tgt),
                                              params.variable_dtype.activation_dtype), []), txt_tgt.size)

    if params.use_video:

        if params.use_discrete_video_loss:

            mak_per_head_shape = mtf.Shape([params.batch_dim, params.sequence_per_head_dim, params.head_dim])
            _vid_msk_tgt = reshape(vid_msk_tgt, new_shape=mak_per_head_shape)
            _cat_msk_tgt = reshape(cat_msk_tgt, new_shape=mak_per_head_shape)

            video_size = constant_scalar(params, 1 / vid_tgt.size)
            video_target = one_hot(vid_tgt, params.discrete_color_dim, dtype=params.variable_dtype.activation_dtype)
            video_loss = einsum([reduce_logsumexp(frame_out, reduced_dim=params.discrete_color_dim), video_size,
                                 _vid_msk_tgt, _cat_msk_tgt], output_shape=[params.head_dim])
            video_loss = add(video_loss,
                             einsum([frame_out, video_target, video_size, constant_scalar(params, -1),
                                     _vid_msk_tgt, _cat_msk_tgt], output_shape=[params.head_dim]))
            video_loss = reduce_sum(video_loss, output_shape=[])

        else:
            size = constant_scalar(params, 1 / frame_out.size)
            out = add(frame_out, negative(vid_tgt))
            video_loss: mtf.Tensor = einsum([out, vid_msk_tgt, cat_msk_tgt, size, sign(out)], output_shape=[])

        loss_list.append(video_loss)

        if vid_msk_tgt is not None:
            video_loss = einsum([constant_scalar(params, vid_msk_tgt.size), reciprocal(reduce_sum(vid_msk_tgt)),
                                 constant_scalar(params, cat_msk_tgt.size), reciprocal(reduce_sum(cat_msk_tgt)),
                                 video_loss], output_shape=[])
    return loss_list, token_loss, accuracy, video_loss


def _build(params: ModelParameter,
           vid: typing.Optional[mtf.Tensor],
           cat_msk_src: typing.Optional[mtf.Tensor],
           cat_msk_tgt: typing.Optional[mtf.Tensor],
           txt_src: typing.Optional[mtf.Tensor],
           txt_tgt: typing.Optional[mtf.Tensor],
           vid_msk_src: typing.Optional[mtf.Tensor],
           vid_msk_tgt: typing.Optional[mtf.Tensor],
           txt_msk: typing.Optional[mtf.Tensor]):
    cat_msk_src = _default_ones(params, cat_msk_src)
    cat_msk_tgt = _default_ones(params, cat_msk_tgt)
    vid_msk_src = _default_ones(params, vid_msk_src)
    vid_msk_tgt = _default_ones(params, vid_msk_tgt)

    loss_list = []
    spatial_ctx: mtf.Dimension = txt_tgt.shape[-2] if params.use_language else vid.shape[2]

    src, vid_tgt = scoped("input", _input, params, vid, cat_msk_src, txt_src, vid_msk_src, spatial_ctx)
    out = scoped("body", _body, params, src)
    frame_out, token_out = scoped("output", _output, params, out, spatial_ctx)
    loss_list, token_loss, accuracy, video_loss = scoped("loss", _loss, params, frame_out, token_out, txt_tgt,
                                                         loss_list, vid_msk_tgt, cat_msk_tgt, vid_tgt)

    params.attention_idx = 0

    return add_n(loss_list), loss_list, video_loss, accuracy, token_loss, frame_out, token_out


def build(params: ModelParameter,
          vid: typing.Optional[mtf.Tensor],
          cat_msk_src: typing.Optional[mtf.Tensor],
          cat_msk_tgt: typing.Optional[mtf.Tensor],
          txt_src: typing.Optional[mtf.Tensor],
          txt_tgt: typing.Optional[mtf.Tensor],
          vid_msk_src: typing.Optional[mtf.Tensor],
          vid_msk_tgt: typing.Optional[mtf.Tensor],
          txt_msk: typing.Optional[mtf.Tensor],
          ) -> typing.Tuple[mtf.Tensor, typing.List, mtf.Tensor, typing.Optional[mtf.Tensor],
                            mtf.Tensor, mtf.Tensor, mtf.Tensor]:
    """
    Build Mesh Tensorflow graph of a model given parameters previously inserted.
    The model slices the video input itself (to save on TPU CPU <--> TPU Core bandwidth), but needs both
    text source and text target.
    :param params: Instance of ModelParameter for which to build the graph
    :param vid: Optional Video to attend over, length=(context+1)
    :param cat_msk_src: Optional mask for zero frames
    :param cat_msk_tgt: Optional mask to remove loss for certain video frames
    :param txt_src: Optional tokenized text source, will be embedded
    :param txt_tgt: Optional tokenized text target, required when source is given
    :param vid_msk_src: Optional mask for zero frames
    :param vid_msk_tgt: Optional mask to remove loss for certain video frames
    :param txt_msk: Optional mask to remove loss for certain token positions
    :return: (Generated Video, Total Loss, Video Loss, Token Loss)
    """
    with mtf.utils.outside_all_rewrites():
        return scoped(params.model_mode, _build, params, vid, cat_msk_src, cat_msk_tgt, txt_src, txt_tgt, vid_msk_src,
                      vid_msk_tgt, txt_msk)
