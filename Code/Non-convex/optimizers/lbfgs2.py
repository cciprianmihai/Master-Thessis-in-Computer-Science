from functools import reduce
from math import isinf

import torch
from torch.optim import Optimizer

import numpy as np

class LBFGS2(Optimizer):
    """Implements L-BFGS algorithm.
    .. warning::
        This optimizer doesn't support per-parameter options and parameter
        groups (there can be only one).
    .. warning::
        Right now all parameters have to be on a single device. This will be
        improved in the future.
    .. note::
        This is a very memory intensive optimizer (it requires additional
        ``param_bytes * (history_size + 1)`` bytes). If it doesn't fit in memory
        try reducing the history size, or use a different algorithm.
    Arguments:
        lr (float): learning rate (default: 1)
        max_iter (int): maximal number of iterations per optimization step
            (default: 20)
        max_eval (int): maximal number of function evaluations per optimization
            step (default: max_iter * 1.25).
        tolerance_grad (float): termination tolerance on first order optimality
            (default: 1e-5).
        tolerance_change (float): termination tolerance on function
            value/parameter changes (default: 1e-9).
        history_size (int): update history size (default: 100).
    """

    def __init__(self, params, lr=1, max_iter=20, max_eval=None,
                 tolerance_grad=1e-5, tolerance_change=1e-9, history_size=100,
                 line_search_fn=None, a_1=0.0, a_2=1.0):
        if max_eval is None:
            max_eval = max_iter * 5 // 4
        defaults = dict(lr=lr, max_iter=max_iter, max_eval=max_eval,
                        tolerance_grad=tolerance_grad, tolerance_change=tolerance_change,
                        history_size=history_size, line_search_fn=line_search_fn, a_1=a_1, a_2=a_2)
        super(LBFGS2, self).__init__(params, defaults)

        if len(self.param_groups) != 1:
            raise ValueError("LBFGS doesn't support per-parameter options "
                             "(parameter groups)")

        self._params = self.param_groups[0]['params']
        self._numel_cache = None

    def _numel(self):
        if self._numel_cache is None:
            self._numel_cache = reduce(lambda total, p: total + p.numel(), self._params, 0)
        return self._numel_cache

    def _gather_flat_grad(self):
        views = []
        for p in self._params:
            if p.grad is None:
                view = p.data.new(p.data.numel()).zero_()
            elif p.grad.data.is_sparse:
                view = p.grad.data.to_dense().view(-1)
            else:
                view = p.grad.data.view(-1)
            views.append(view)
        return torch.cat(views, 0)

    def _add_grad(self, step_size, update):
        offset = 0
        for p in self._params:
            numel = p.numel()
            # view as to avoid deprecated pointwise semantics
            p.data.add_(step_size, update[offset:offset + numel].view_as(p.data))
            offset += numel
        assert offset == self._numel()

    def step(self, closure):
        """Performs a single optimization step.
        Arguments:
            closure (callable): A closure that reevaluates the model
                and returns the loss.
        """
        assert len(self.param_groups) == 1

        group = self.param_groups[0]
        lr = group['lr']
        max_iter = group['max_iter']
        max_eval = group['max_eval']
        tolerance_grad = group['tolerance_grad']
        tolerance_change = group['tolerance_change']
        line_search_fn = group['line_search_fn']
        history_size = group['history_size']
        a_1 = group['a_1']
        a_2 = group['a_2']
        # NOTE: LBFGS has only global state, but we register it as state for
        # the first param, because this helps with casting in load_state_dict
        state = self.state[self._params[0]]
        state.setdefault('func_evals', 0)
        state.setdefault('n_iter', 0)

        # evaluate initial f(x) and df/dx
        orig_loss = closure()
        loss = float(orig_loss)
        current_evals = 1
        state['func_evals'] += 1

        flat_grad = self._gather_flat_grad()
        abs_grad_sum = flat_grad.abs().sum()

        if abs_grad_sum <= tolerance_grad:
            return orig_loss

        # tensors cached in state (for tracing)
        d = state.get('d')
        t = state.get('t')
        old_dirs = state.get('old_dirs')
        old_stps = state.get('old_stps')
        H_diag = state.get('H_diag')
        prev_flat_grad = state.get('prev_flat_grad')
        prev_loss = state.get('prev_loss')

        n_iter = 0
        # optimize for a max of max_iter iterations
        while n_iter < max_iter:
            # keep track of nb of iterations
            n_iter += 1
            state['n_iter'] += 1

            ############################################################
            # compute gradient descent direction
            ############################################################
            if state['n_iter'] == 1:
                d = flat_grad.neg()
                old_dirs = []
                old_stps = []
                H_diag = 1
            else:
                # do lbfgs update (update memory)
                y = flat_grad.sub(prev_flat_grad)
                s = d.mul(t)
                ys = y.dot(s)  # y*s
                if ys > 1e-10:
                    # updating memory
                    if len(old_dirs) == history_size:
                        # shift history by one (limited-memory)
                        old_dirs.pop(0)
                        old_stps.pop(0)

                    # store new direction/step
                    old_dirs.append(y)
                    old_stps.append(s)

                    # update scale of initial Hessian approximation
                    H_diag = ys / y.dot(y)  # (y*y)

                # compute the approximate (L-BFGS) inverse Hessian
                # multiplied by the gradient
                num_old = len(old_dirs)

                if 'ro' not in state:
                    state['ro'] = [None] * history_size
                    state['al'] = [None] * history_size
                ro = state['ro']
                al = state['al']

                for i in range(num_old):
                    ro[i] = 1. / old_dirs[i].dot(old_stps[i])

                # iteration in L-BFGS loop collapsed to use just one buffer
                q = flat_grad.neg()
                for i in range(num_old - 1, -1, -1):
                    al[i] = old_stps[i].dot(q) * ro[i]
                    q.add_(-al[i], old_dirs[i])

                # multiply by initial Hessian
                # r/d is the final direction
                d = r = torch.mul(q, H_diag)
                for i in range(num_old):
                    be_i = old_dirs[i].dot(r) * ro[i]
                    r.add_(al[i] - be_i, old_stps[i])

            if prev_flat_grad is None:
                prev_flat_grad = flat_grad.clone()
            else:
                prev_flat_grad.copy_(flat_grad)
            prev_loss = loss

            ############################################################
            # compute step length
            ############################################################
            # reset initial guess for step size
            if state['n_iter'] == 1:
                t = min(1., 1. / abs_grad_sum) * lr
            else:
                t = lr

            # directional derivative
            gtd = flat_grad.dot(d)  # g * d

            # optional line search: user function
            ls_func_evals = 0
            if line_search_fn is not None:
                # perform line search, using user function
                if line_search_fn == 'weak_wolfe':
                    t = self._line_search_weak_wolfe(closure, d, a_1, a_2)
                elif line_search_fn == 'goldstein':
                    t = self._line_search_goldstein(closure, d, a_1, a_2)
                elif line_search_fn == 'backtracking':
                    t = self._line_search_backtracking(closure, d, a_2)
                self._add_grad(t, d)
            else:
                # no line search, simply move with fixed-step
                self._add_grad(t, d)
            if n_iter != max_iter:
                # re-evaluate function only if not in last iteration
                # the reason we do this: in a stochastic setting,
                # no use to re-evaluate that function here
                loss = float(closure())
                flat_grad = self._gather_flat_grad()
                abs_grad_sum = flat_grad.abs().sum()
                ls_func_evals = 1

            # update func eval
            current_evals += ls_func_evals
            state['func_evals'] += ls_func_evals

            ############################################################
            # check conditions
            ############################################################
            if n_iter == max_iter:
                break

            if current_evals >= max_eval:
                break

            if abs_grad_sum <= tolerance_grad:
                break

            if gtd > -tolerance_change:
                break

            if d.mul(t).abs_().sum() <= tolerance_change:
                break

            if abs(loss - prev_loss) < tolerance_change:
                break

        state['d'] = d
        state['t'] = t
        state['old_dirs'] = old_dirs
        state['old_stps'] = old_stps
        state['H_diag'] = H_diag
        state['prev_flat_grad'] = prev_flat_grad
        state['prev_loss'] = prev_loss

        return orig_loss, t

    def _save_model_parameters(self):
        original_param_data_list = []
        for p in self._params:
            param_data = p.data.new(p.size())
            param_data.copy_(p.data)
            original_param_data_list.append(param_data)
        return original_param_data_list

    def _set_param(self, param_data_list):
        for i in range(len(param_data_list)):
            self._params[i].data.copy_(param_data_list[i])

    def _update_model_parameters(self, alpha, d):
        offset = 0
        for p in self._params:
            numel = p.numel()
            p.data.copy_(
                p.data + alpha * d[offset:offset + numel].resize_(p.size()))
            offset += numel
        assert offset == self._numel()

    def _directional_derivative(self, d):
        deriv = 0.0
        offset = 0
        for p in self._params:
            numel = p.numel()
            deriv += torch.sum(
                p.grad.data * d[offset:offset + numel].resize_(p.size()))
            offset += numel
        assert offset == self._numel()
        return deriv

    def _line_search_backtracking(self, closure, d, alpha_k):
        """
        Back tracking line search method with the following preconditions:
            1. 0 < rho < 0.5
            2. 0 < w < 1
        """
        rho = 1e-4
        w = 0.5
        # Save initial model parameters
        initial_model_parameters = self._save_model_parameters()
        # Compute initial loss
        f_0 = closure().item()
        # Compute the directional derivative
        f_0_prime = self._directional_derivative(d)
        # While conditions are True
        while True:
            # Update parameters with the value of alpha_k
            self._update_model_parameters(alpha_k, d)
            # Calculate the loss of the new model
            f_k = closure().item()
            # Restore the initial model
            self._set_param(initial_model_parameters)
            # If conditions are met, we stop
            if f_k <= f_0 + rho * alpha_k * f_0_prime:
                break
            # Else we update alpha_k for a new iteration
            else:
                alpha_k *= w
        # Return alpha_k - step size
        return alpha_k

    def _line_search_goldstein(self, closure, d, a_1, a_2):
        """
        Goldstein line search method with the following preconditions:
            1. 0 < rho < 0.5
            2. t > 1
        """
        a_1 = 0.0
        a_2 = np.inf
        rho = 1e-4
        t = 2.0
        # Save initial model parameters
        initial_model_parameters = self._save_model_parameters()
        # Compute initial loss
        f_0 = closure().item()
        # Compute the directional derivative
        f_0_prime = self._directional_derivative(d)
        # Set initial alpha_k
        alpha_k = min(1e4, (a_1 + a_2) / 2.0)
        # While conditions are True
        while True:
            # Update parameters with the value of alpha_k
            self._update_model_parameters(alpha_k, d)
            # Calculate the loss of the new model
            f_k = closure().item()
            # Restore the initial model
            self._set_param(initial_model_parameters)
            # If conditions are met, we stop, else we update alpha_k for a new iteration
            if f_k <= f_0 + rho * alpha_k * f_0_prime:
                if f_k >= f_0 + (1 - rho) * alpha_k * f_0_prime:
                    break
                else:
                    a_1 = alpha_k
                    alpha_k = t * alpha_k if isinf(a_2) else (a_1 + a_2) / 2.0
            else:
                a_2 = alpha_k
                alpha_k = (a_1 + a_2) / 2.0
            if torch.sum(torch.abs(alpha_k * d)) < self.param_groups[0]['tolerance_grad']:
                break
            if abs(a_2 - a_1) < 1e-6:
                break
        # Return alpha_k - step size
        return alpha_k

    def _line_search_weak_wolfe(self, closure, d, a_1, a_2):
        """
        Weak Wolfe line search method with the following preconditions:
            1. 0 < rho < 0.5
            2. rho < sigma < 1
        """
        a_1 = 0.0
        a_2 = np.inf
        rho = 1e-4
        sigma = 0.9
        # Save initial model parameters
        initial_model_parameters = self._save_model_parameters()
        # Compute initial loss
        f_0 = closure().item()
        # Compute the directional derivative
        f_0_prime = self._directional_derivative(d)
        # Set initial alpha_k
        alpha_k = min(1e4, (a_1 + a_2) / 2.0)
        # While conditions are True
        while True:
            # Update parameters with the value of alpha_k
            self._update_model_parameters(alpha_k, d)
            # Calculate the loss of the new model
            f_k = closure().item()
            # Compute the directional derivative
            f_k_prime = self._directional_derivative(d)
            # Restore the initial model
            self._set_param(initial_model_parameters)
            # If conditions are met, we stop, else we update alpha_k for a new iteration
            if f_k <= f_0 + rho * alpha_k * f_0_prime:
                if f_k_prime >= sigma * f_0_prime:
                    break
                else:
                    alpha_hat = alpha_k + (alpha_k - a_1) * f_k_prime / (f_0_prime - f_k_prime)
                    a_1 = alpha_k
                    f_0 = f_k
                    f_0_prime = f_k_prime
                    alpha_k = alpha_hat
            else:
                alpha_hat = a_1 + 0.5 * (alpha_k - a_1) / (1 + (f_0 - f_k) / ((alpha_k - a_1) * f_0_prime))
                a_2 = alpha_k
                alpha_k = alpha_hat
            # We check, also, the tolerance grad
            if torch.sum(torch.abs(alpha_k * d)) < self.param_groups[0]['tolerance_grad']:
                break
            if abs(a_2 - a_1) < 1e-6:
                break
        # Return alpha_k - step size
        return alpha_k

    def _gather_flat_grad(self):
        views = []
        for p in self._params:
            if p.grad is None:
                view = p.data.new(p.data.numel()).zero_()
            elif p.grad.data.is_sparse:
                view = p.grad.data.to_dense().view(-1)
            else:
                view = p.grad.data.view(-1)
            views.append(view)
        return torch.cat(views, 0)

    def _gather_flat_data(self):
        views = []
        for p in self._params:
            view = p.data.view(-1)
            views.append(view)
        return torch.cat(views, 0)

    def _grad_norm(self):
        flat_grad = self._gather_flat_grad()
        return flat_grad.norm()
