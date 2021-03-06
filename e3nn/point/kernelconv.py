import torch

import e3nn.o3 as o3
import e3nn.rs as rs
from e3nn.kernel import Kernel


class KernelConv(Kernel):
    def __init__(self, Rs_in, Rs_out, RadialModel, get_l_filters=o3.selection_rule, sh=o3.spherical_harmonics_xyz, normalization='norm'):
        """
        :param Rs_in: list of triplet (multiplicity, representation order, parity)
        :param Rs_out: list of triplet (multiplicity, representation order, parity)
        :param RadialModel: Class(d), trainable model: R -> R^d
        :param get_l_filters: function of signature (l_in, l_out) -> [l_filter]
        :param sh: spherical harmonics function of signature ([l_filter], xyz[..., 3]) -> Y[m, ...]
        :param normalization: either 'norm' or 'component'
        representation order = nonnegative integer
        parity = 0 (no parity), 1 (even), -1 (odd)
        """
        super(KernelConv, self).__init__(Rs_in, Rs_out, RadialModel, get_l_filters, sh, normalization)

    def forward(self, features, difference_geometry, mask, y=None, radii=None, custom_backward=True):
        """
        :param features: tensor [batch, b, l_in * mul_in * m_in]
        :param difference_geometry: tensor [batch, a, b, xyz]
        :param mask:     tensor [batch, a] (In order to zero contributions from padded atoms.)
        :param y:        Optional precomputed spherical harmonics.
        :param radii:    Optional precomputed normed geometry.
        :param custom_backward: call KernelConvFn rather than using automatic differentiation, (default True)
        :return:         tensor [batch, a, l_out * mul_out * m_out]
        """
        batch, a, b, xyz = difference_geometry.size()
        assert xyz == 3

        # precompute all needed spherical harmonics
        if y is None:
            y = self.sh(self.set_of_l_filters, difference_geometry)  # [l_filter * m_filter, batch, a, b]

        # use the radial model to fix all the degrees of freedom
        # note: for the normalization we assume that the variance of R[i] is one
        if radii is None:
            radii = difference_geometry.norm(2, dim=-1)  # [batch, a, b]
        r = self.R(radii.flatten()).view(
            *radii.shape, -1
        )  # [batch, a, b, l_out * l_in * mul_out * mul_in * l_filter]

        norm_coef = getattr(self, 'norm_coef')
        norm_coef = norm_coef[:, :, (radii == 0).type(torch.long)]  # [l_out, l_in, batch, a, b]

        if custom_backward:
            kernel_conv = KernelConvFn.apply(
                features, y, r, norm_coef, self.Rs_in, self.Rs_out, self.get_l_filters, self.set_of_l_filters
            )
        else:
            kernel_conv = kernel_conv_fn_forward(
                features, y, r, norm_coef, self.Rs_in, self.Rs_out, self.get_l_filters, self.set_of_l_filters
            )

        return kernel_conv * mask.unsqueeze(-1)


def kernel_conv_fn_forward(F, Y, R, norm_coef, Rs_in, Rs_out, get_l_filters, set_of_l_filters):
    """
    :param F: tensor [batch, b, l_in * mul_in * m_in]
    :param Y: tensor [l_filter * m_filter, batch, a, b]
    :param R: tensor [batch, a, b, l_out * l_in * mul_out * mul_in * l_filter]
    :param norm_coef: tensor [l_out, l_in, batch, a, b]
    :return: tensor [batch, a, l_out * mul_out * m_out, l_in * mul_in * m_in]
    """
    batch, a, b = Y.shape[1:]
    n_in = rs.dim(Rs_in)
    n_out = rs.dim(Rs_out)

    kernel_conv = Y.new_zeros(batch, a, n_out)

    # note: for the normalization we assume that the variance of R[i] is one
    begin_R = 0

    begin_out = 0
    for i, (mul_out, l_out, p_out) in enumerate(Rs_out):
        s_out = slice(begin_out, begin_out + mul_out * (2 * l_out + 1))
        begin_out += mul_out * (2 * l_out + 1)

        begin_in = 0
        for j, (mul_in, l_in, p_in) in enumerate(Rs_in):
            s_in = slice(begin_in, begin_in + mul_in * (2 * l_in + 1))
            begin_in += mul_in * (2 * l_in + 1)

            l_filters = get_l_filters(l_in, p_in, l_out, p_out)
            if not l_filters:
                continue

            # extract the subset of the `R` that corresponds to the couple (l_out, l_in)
            n = mul_out * mul_in * len(l_filters)
            sub_R = R[:, :, :, begin_R: begin_R + n].contiguous().view(
                batch, a, b, mul_out, mul_in, -1
            )  # [batch, a, b, mul_out, mul_in, l_filter]
            begin_R += n

            sub_norm_coef = norm_coef[i, j]  # [batch]

            K = 0
            for k, l_filter in enumerate(l_filters):
                offset = sum(2 * l + 1 for l in set_of_l_filters if l < l_filter)
                sub_Y = Y[offset: offset + 2 * l_filter + 1, ...]  # [m, batch, a, b]

                C = o3.clebsch_gordan(l_out, l_in, l_filter, cached=True, like=kernel_conv)  # [m_out, m_in, m]

                K += torch.einsum(
                    "ijk,kzab,zabuv,zab,zbvj->zaui",
                    C, sub_Y, sub_R[..., k], sub_norm_coef, F[..., s_in].view(batch, b, mul_in, -1)
                )  # [batch, a, mul_out, m_out]

            if K is not 0:
                kernel_conv[:, :, s_out] += K.view(batch, a, -1)

    return kernel_conv


class KernelConvFn(torch.autograd.Function):
    @staticmethod
    def forward(ctx, F, Y, R, norm_coef, Rs_in, Rs_out, get_l_filters, set_of_l_filters):
        f"""{kernel_conv_fn_forward.__doc__}"""
        ctx.batch, ctx.a, ctx.b = Y.shape[1:]
        ctx.Rs_in = Rs_in
        ctx.Rs_out = Rs_out
        ctx.get_l_filters = get_l_filters
        ctx.set_of_l_filters = set_of_l_filters

        # save necessary tensors for backward
        saved_Y = saved_R = saved_F = None
        if F.requires_grad:
            ctx.F_shape = F.shape
            saved_R = R
            saved_Y = Y
        if Y.requires_grad:
            ctx.Y_shape = Y.shape
            saved_R = R
            saved_F = F
        if R.requires_grad:
            ctx.R_shape = R.shape
            saved_Y = Y
            saved_F = F
        ctx.save_for_backward(saved_F, saved_Y, saved_R, norm_coef)

        return kernel_conv_fn_forward(
            F, Y, R, norm_coef, ctx.Rs_in, ctx.Rs_out, ctx.get_l_filters, ctx.set_of_l_filters
        )

    @staticmethod
    def backward(ctx, grad_kernel):
        F, Y, R, norm_coef = ctx.saved_tensors
        batch, a, b = ctx.batch, ctx.a, ctx.b

        grad_F = grad_Y = grad_R = None

        if ctx.needs_input_grad[0]:
            grad_F = grad_kernel.new_zeros(*ctx.F_shape)  # [batch, b, l_in * mul_in * m_in]
        if ctx.needs_input_grad[1]:
            grad_Y = grad_kernel.new_zeros(*ctx.Y_shape)  # [l_filter * m_filter, batch, a, b]
        if ctx.needs_input_grad[2]:
            grad_R = grad_kernel.new_zeros(*ctx.R_shape)  # [batch, a, b, l_out * l_in * mul_out * mul_in * l_filter]

        begin_R = 0

        begin_out = 0
        for i, (mul_out, l_out, p_out) in enumerate(ctx.Rs_out):
            s_out = slice(begin_out, begin_out + mul_out * (2 * l_out + 1))
            begin_out += mul_out * (2 * l_out + 1)

            begin_in = 0
            for j, (mul_in, l_in, p_in) in enumerate(ctx.Rs_in):
                s_in = slice(begin_in, begin_in + mul_in * (2 * l_in + 1))
                begin_in += mul_in * (2 * l_in + 1)

                l_filters = ctx.get_l_filters(l_in, p_in, l_out, p_out)
                if not l_filters:
                    continue

                n = mul_out * mul_in * len(l_filters)
                if (grad_Y is not None) or (grad_F is not None):
                    sub_R = R[:, :, :, begin_R: begin_R + n].contiguous().view(
                        batch, a, b, mul_out, mul_in, -1
                    )  # [batch, a, b, mul_out, mul_in, l_filter]
                if grad_R is not None:
                    sub_grad_R = grad_R[:, :, :, begin_R: begin_R + n].contiguous().view(
                        batch, a, b, mul_out, mul_in, -1
                    )  # [batch, a, b, mul_out, mul_in, l_filter]

                if grad_F is not None:
                    sub_grad_F = grad_F[:, :, s_in].contiguous().view(
                        batch, b, mul_in, 2 * l_in + 1
                    )  # [batch, b, mul_in, 2 * l_in + 1]
                if (grad_Y is not None) or (grad_R is not None):
                    sub_F = F[..., s_in].view(batch, b, mul_in, 2 * l_in + 1)

                grad_K = grad_kernel[:, :, s_out].view(
                    batch, a, mul_out, 2 * l_out + 1
                )

                sub_norm_coef = norm_coef[i, j]  # [batch, a, b]

                for k, l_filter in enumerate(l_filters):
                    tmp = sum(2 * l + 1 for l in ctx.set_of_l_filters if l < l_filter)
                    C = o3.clebsch_gordan(l_out, l_in, l_filter, cached=True, like=grad_kernel)  # [m_out, m_in, m]

                    if (grad_F is not None) or (grad_R is not None):
                        sub_Y = Y[tmp: tmp + 2 * l_filter + 1, ...]  # [m, batch, a, b]

                    if grad_F is not None:
                        sub_grad_F += torch.einsum(
                            "zaui,ijk,kzab,zabuv,zab->zbvj",
                            grad_K, C, sub_Y, sub_R[..., k], sub_norm_coef
                        )  # [batch, b, mul_in, 2 * l_in + 1
                    if grad_Y is not None:
                        grad_Y[tmp: tmp + 2 * l_filter + 1, ...] += torch.einsum(
                            "zaui,ijk,zabuv,zab,zbvj->kzab",
                            grad_K, C, sub_R[..., k], sub_norm_coef, sub_F
                        )  # [m, batch, a, b]
                    if grad_R is not None:
                        sub_grad_R[..., k] = torch.einsum(
                            "zaui,ijk,kzab,zab,zbvj->zabuv",
                            grad_K, C, sub_Y, sub_norm_coef, sub_F
                        )  # [batch, a, b, mul_out, mul_in]
                if grad_F is not None:
                    grad_F[:, :, s_in] = sub_grad_F.view(batch, b, mul_in * (2 * l_in + 1))
                if grad_R is not None:
                    grad_R[..., begin_R: begin_R + n] += sub_grad_R.view(batch, a, b, -1)
                begin_R += n

        return grad_F, grad_Y, grad_R, None, None, None, None, None
