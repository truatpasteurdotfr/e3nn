# pylint: disable=C,E1101,E1102
import unittest
from functools import partial
from itertools import product

import torch

from e3nn.kernel import Kernel
from e3nn.radial import ConstantRadialModel
from e3nn.point.operations import Convolution
from e3nn.point.kernelconv import KernelConv, KernelConvFn
from e3nn.rs import dim


class TestKernelConvFn(unittest.TestCase):
    def test1(self):
        if torch.cuda.is_available():
            device = torch.device('cuda')
        else:
            device = torch.device('cpu')

        torch.set_default_dtype(torch.float64)
        Rs_in = [(1, 0), (1, 1), (2, 0), (1, 2)]
        Rs_out = [(2, 0), (1, 1), (1, 2), (3, 0)]
        KC = KernelConv(Rs_in, Rs_out, ConstantRadialModel).to(device)

        n_path = 0
        for mul_out, l_out, p_out in KC.Rs_out:
            for mul_in, l_in, p_in in KC.Rs_in:
                l_filters = KC.get_l_filters(l_in, p_in, l_out, p_out)
                n_path += mul_out * mul_in * len(l_filters)

        batch = 2
        atoms = 3

        true_false = [False, True]
        options = set(product(true_false, repeat=3)) - {(False, False, False)}
        for rg_F, rg_Y, rg_R in options:
            F = torch.randn(batch, atoms, dim(Rs_in), requires_grad=rg_F).to(device)
            geo = torch.randn(batch, atoms, 3)
            r = (geo.unsqueeze(1) - geo.unsqueeze(2)).to(device)
            radii = r.norm(batch, dim=-1)  # [batch, a, b]
            Y = KC.sh(KC.set_of_l_filters, r)  # [l_filter * m_filter, batch, a, b]
            Y = Y.clone().detach().requires_grad_(rg_Y).to(device)
            R = torch.randn(
                batch, atoms, atoms, n_path, requires_grad=rg_R
            ).to(device)  # [batch, a, b, l_out * l_in * mul_out * mul_in * l_filter]
            norm_coef = KC.norm_coef
            norm_coef = norm_coef[:, :, (radii == 0).type(torch.long)].to(device)  # [l_out, l_in, batch, a, b]

            inputs = (
                F, Y, R, norm_coef, KC.Rs_in, KC.Rs_out, KC.get_l_filters, KC.set_of_l_filters
            )
            self.assertTrue(torch.autograd.gradcheck(KernelConvFn.apply, inputs))


class TestKernelConv(unittest.TestCase):
    def setUp(self):
        super(TestKernelConv, self).setUp()
        torch.set_default_dtype(torch.float64)
        torch.backends.cudnn.deterministic = True
        self.Rs_in = [(1, 0), (1, 1), (2, 0), (1, 2)]
        self.Rs_out = [(2, 0), (1, 1), (1, 2), (3, 0)]

        batch = 100
        atoms = 40
        self.geometry = torch.rand(batch, atoms, 3)
        rb = self.geometry.unsqueeze(1)  # [batch, 1, b, xyz]
        ra = self.geometry.unsqueeze(2)  # [batch, a, 1, xyz]
        self.r = rb - ra
        self.features = torch.rand(batch, atoms, dim(self.Rs_in), requires_grad=True)
        self.mask = torch.ones(batch, atoms)

        self.msg = "Kernel or Convolution parameters were not identical. This means the test cannot compare outputs."

    def get_kernel_conv_kernelconv(self, seed, normalization):
        torch.manual_seed(seed)
        K = partial(Kernel, RadialModel=ConstantRadialModel, normalization=normalization)
        C = Convolution(K, self.Rs_in, self.Rs_out)

        torch.manual_seed(seed)
        KC = KernelConv(self.Rs_in, self.Rs_out, RadialModel=ConstantRadialModel, normalization=normalization)
        return K, C, KC

    def ensure_parameters_same(self, conv, kernel_conv):
        assert all(torch.all(a == b) for a, b in zip(conv.kernel.parameters(), kernel_conv.parameters())), self.msg

    def test_compare_forward(self):
        for normalization in ["norm", "component"]:
            K, C, KC = self.get_kernel_conv_kernelconv(0, normalization)
            new_features = C(self.features, self.geometry) * self.mask.unsqueeze(dim=-1)
            check_new_features = KC(self.features, self.r, self.mask)

            self.ensure_parameters_same(C, KC)
            self.assertTrue(torch.allclose(new_features, check_new_features))

    def test_compare_backward(self):
        check_features = self.features.clone().detach().requires_grad_()
        check_r = self.r.clone().detach()
        check_mask = self.mask.clone().detach()

        for normalization in ["norm", "component"]:
            K, C, KC = self.get_kernel_conv_kernelconv(0, normalization)
            new_features = C(self.features, self.geometry) * self.mask.unsqueeze(dim=-1)
            check_new_features = KC(check_features, check_r, check_mask)

            self.ensure_parameters_same(C, KC)

            # Capture ground truth gradient
            target = torch.rand_like(new_features)
            loss = torch.norm(new_features - target)
            loss.backward()

            # Capture KernelConv gradient
            check_target = target.clone().detach()
            check_loss = torch.norm(check_new_features - check_target)
            check_loss.backward()
            self.assertTrue(torch.allclose(self.features.grad, check_features.grad))

    def test_compare_custom_backward(self):
        check_features = self.features.clone().detach().requires_grad_()
        check_r = self.r.clone().detach()
        check_mask = self.mask.clone().detach()

        for normalization in ["norm", "component"]:
            K, C, KC = self.get_kernel_conv_kernelconv(0, normalization)
            new_features = C(self.features, self.geometry) * self.mask.unsqueeze(dim=-1)
            check_new_features = KC(check_features, check_r, check_mask, custom_backward=True)

            self.ensure_parameters_same(C, KC)

            # Capture ground truth gradient
            target = torch.rand_like(new_features)
            loss = torch.norm(new_features - target)
            loss.backward()

            # Capture KernelConv gradient
            check_target = target.clone().detach()
            check_loss = torch.norm(check_new_features - check_target)
            check_loss.backward()
            self.assertTrue(torch.allclose(self.features.grad, check_features.grad))


if __name__ == '__main__':
    unittest.main()
