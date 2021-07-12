import mesh_tensorflow as mtf

from .backend import variable
from .context import OptimizerCtx
from ..mtf_wrapper import (cast, optimizer_scalar, einsum, greater, minimum,
                           reduce_mean, reduce_sum, assign, add, multiply, maximum, reciprocal, square,
                           reduce_max, rsqrt, sqrt, add_n, negative, pow as mtf_pow)
from ..utils_mtf import weighted_add


def opt_rsqrt(tensor: mtf.Tensor) -> mtf.Tensor:
    return reciprocal(maximum(sqrt(tensor), 1e-5))


def debias_momentum(ctx: OptimizerCtx, momentum: mtf.Tensor) -> mtf.Tensor:
    return reciprocal(add(1, negative(mtf_pow(momentum, ctx.step_count))))


def debias(ctx: OptimizerCtx, tensor: mtf.Tensor, momentum: mtf.Tensor) -> mtf.Tensor:
    return multiply(tensor, debias_momentum(ctx, momentum))


def adam(ctx: OptimizerCtx) -> mtf.Tensor:
    exp_avg_p2_ptr = variable(ctx.params, ctx.var, 'exp_avg_p2', ctx.var.shape)
    exp_avg_p1_ptr = variable(ctx.params, ctx.var, 'exp_avg_p1', ctx.var.shape)

    exp_avg_p2 = weighted_add(exp_avg_p2_ptr, square(ctx.grad), ctx.beta2)
    ctx.grad = weighted_add(exp_avg_p1_ptr, ctx.grad, ctx.beta1)

    ctx.update_ops[exp_avg_p2_ptr.name] = exp_avg_p2
    ctx.update_ops[exp_avg_p1_ptr.name] = ctx.grad
    return einsum([opt_rsqrt(debias(ctx, exp_avg_p2, ctx.beta2)), ctx.grad,
                   debias_momentum(ctx, ctx.beta1)], output_shape=ctx.grad.shape)


def novograd(ctx: OptimizerCtx) -> mtf.Tensor:
    exp_avg_p1 = exp_avg_p1_ptr = variable(ctx.params, ctx.var, "exp_avg_p1", ctx.var.shape)
    exp_avg_p2 = exp_avg_p2_ptr = variable(ctx.params, ctx.var, "exp_avg_p2", [])

    exp_avg_p1 = add(multiply(ctx.beta1, exp_avg_p1), multiply(ctx.grad, opt_rsqrt(exp_avg_p2)))
    exp_avg_p2 = weighted_add(exp_avg_p2, reduce_sum(square(ctx.grad)), ctx.beta2)
    ctx.update_ops.extend([assign(exp_avg_p1_ptr, exp_avg_p1),
                           assign(exp_avg_p2_ptr, exp_avg_p2)])
    return add(multiply(ctx.beta1, exp_avg_p1),
               multiply(ctx.grad, opt_rsqrt(debias(ctx, exp_avg_p2, ctx.beta2))))


def sm3(ctx: OptimizerCtx) -> mtf.Tensor:
    weight_update = variable(ctx.params, ctx.var, "dim0", [ctx.var.shape.dims[0]])
    buffer = [weight_update]

    for i in range(1, ctx.var.shape.ndims):
        buffer.append(variable(ctx.params, ctx.var, f"dim{i}", [ctx.var.shape.dims[i]]))
        weight_update = minimum(weight_update, buffer[-1])

    weight_update = add(weight_update, square(ctx.grad))
    for buf_ptr, dim in zip(buffer, weight_update.shape.dims):
        ctx.update_ops[buf_ptr.name] = reduce_max(weight_update, output_shape=[dim])
    return multiply(ctx.grad, opt_rsqrt(weight_update))


def return_grad(ctx: OptimizerCtx) -> mtf.Tensor:
    return ctx.grad


def adaptive_gradient_clipping(ctx: OptimizerCtx, gradient_clip: str) -> mtf.Tensor:
    gradient_clip = float(gradient_clip)
    grd_norm = maximum(sqrt(einsum([ctx.grad, ctx.grad], output_shape=[])), 1e-6)
    wgt_norm = maximum(sqrt(einsum([cast(ctx.var.value, ctx.params.optimizer_calculation_dtype)] * 2, output_shape=[])),
                       0.001)
    return weighted_add(einsum([wgt_norm, reciprocal(grd_norm), optimizer_scalar(ctx.params, gradient_clip), ctx.grad],
                               output_shape=ctx.grad.shape), ctx.grad,
                        cast(greater(multiply(grd_norm, reciprocal(wgt_norm)), gradient_clip),
                             ctx.params.optimizer_calculation_dtype))


def l2norm_gradient_clipping(ctx: OptimizerCtx, gradient_clip: str) -> mtf.Tensor:
    gradient_clip = float(gradient_clip)
    return einsum([ctx.grad, optimizer_scalar(ctx.params, gradient_clip),
                   rsqrt(maximum(einsum([ctx.grad, ctx.grad], []), gradient_clip ** -2))])


def global_l2norm_gradient_clipping(ctx: OptimizerCtx, gradient_clip: str) -> mtf.Tensor:
    gradient_clip = float(gradient_clip)
    if ctx.global_norm_reciprocal is None:
        global_sum = add_n([reduce_sum(square(grad)) for grad in ctx.variable_to_gradient.values()])
        ctx.global_norm_reciprocal = rsqrt(maximum(global_sum, gradient_clip ** -2))
    return einsum([ctx.grad, optimizer_scalar(ctx.params, gradient_clip), ctx.global_norm_reciprocal])


def value_gradient_clipping(ctx: OptimizerCtx, gradient_clip: str) -> mtf.Tensor:
    gradient_clip = float(gradient_clip)
    return maximum(minimum(ctx.grad, gradient_clip), -gradient_clip)


def gradient_centralisation(ctx: OptimizerCtx) -> mtf.Tensor:
    return ctx.grad - reduce_mean(ctx.grad)


def weight_centralisation(ctx: OptimizerCtx) -> mtf.Tensor:
    return add(ctx.grad, reduce_mean(ctx.var.value))


def multiply_learning_rate(ctx: OptimizerCtx) -> mtf.Tensor:
    return multiply(ctx.grad, ctx.learning_rate)


OPTIMIZERS = {"adam": adam,
              "sm3": sm3,
              "novograd": novograd,
              "sgd": return_grad,
              "adaptive_clip": adaptive_gradient_clipping,
              "l2norm_clip": l2norm_gradient_clipping,
              "value_clip": value_gradient_clipping,
              "gradient_centralisation": gradient_centralisation,
              "weight_centralisation": weight_centralisation,
              "learning_rate": multiply_learning_rate,
              "global_l2norm_clip": global_l2norm_gradient_clipping
              }
