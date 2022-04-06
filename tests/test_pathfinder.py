"""Test the pathfinder algorithm."""
from jax.config import config

config.update("jax_enable_x64", True)

import pytest
import numpy as np
import chex
import jax
import jax.numpy as jnp
import jax.scipy.stats as stats
import functools
from absl.testing import absltest, parameterized
from jax._src.scipy.optimize._lbfgs import _two_loop_recursion
from blackjax.vi.pathfinder import (
    minimize_lbfgs,
    lbfgs_inverse_hessian_factors,
    lbfgs_inverse_hessian_formula_1,
    lbfgs_inverse_hessian_formula_2,
    lbfgs_sample,
)
from blackjax.kernels import pathfinder
from jax.flatten_util import ravel_pytree


class PathfinderTest(chex.TestCase):
    @parameterized.parameters(
        [(1, 10), (10, 1), (10, 20)],
    )
    def test_inverse_hessian(self, maxiter, maxcor):
        """Test if dot product between approximate inverse hessian and gradient is
        the same between two loop recursion algorthm of LBFGS and formulas of the
        pathfinder paper"""

        def regression_logprob(scale, coefs, preds, x):
            """Linear regression"""
            logpdf = 0
            logpdf += stats.expon.logpdf(scale, 0, 2)
            logpdf += stats.norm.logpdf(coefs, 3 * jnp.ones(x.shape[-1]), 2)
            y = jnp.dot(x, coefs)
            logpdf += stats.norm.logpdf(preds, y, scale)
            return jnp.sum(logpdf)

        def regression_model():
            key = jax.random.PRNGKey(0)
            rng_key, init_key0, init_key1 = jax.random.split(key, 3)
            x_data = jax.random.normal(init_key0, shape=(1_000, 1))
            y_data = 3 * x_data + jax.random.normal(init_key1, shape=x_data.shape)

            logposterior_fn_ = functools.partial(
                regression_logprob, x=x_data, preds=y_data
            )
            logposterior_fn = lambda x: logposterior_fn_(**x)

            return logposterior_fn

        fn = regression_model()
        b0 = {"scale": 1.0, "coefs": 2.0}
        b0_flatten, unravel_fn = ravel_pytree(b0)
        objective_fn = lambda x: -fn(unravel_fn(x))
        status, history = minimize_lbfgs(
            objective_fn, b0_flatten, maxiter=maxiter, maxcor=maxcor
        )

        i = status.k
        i_offset = maxcor + i

        pk = _two_loop_recursion(status)

        s = jnp.diff(history.x.T).at[:, maxcor - status.k].set(0.0)
        z = jnp.diff(history.g.T).at[:, maxcor - status.k].set(0.0)
        S = jax.lax.dynamic_slice(s, (0, i_offset - maxcor), (2, maxcor))
        Z = jax.lax.dynamic_slice(z, (0, i_offset - maxcor), (2, maxcor))

        alpha_scalar = history.gamma[i_offset]
        alpha = alpha_scalar * jnp.ones(S.shape[0])
        beta, gamma = lbfgs_inverse_hessian_factors(S, Z, alpha)
        inv_hess_1 = lbfgs_inverse_hessian_formula_1(alpha, beta, gamma)
        inv_hess_2 = lbfgs_inverse_hessian_formula_2(alpha, beta, gamma)

        np.testing.assert_array_almost_equal(
            pk, -inv_hess_1 @ history.g[i_offset], decimal=5
        )
        np.testing.assert_array_almost_equal(
            pk, -inv_hess_2 @ history.g[i_offset], decimal=5
        )

    @chex.all_variants(without_device=False, with_pmap=False)
    @parameterized.parameters(
        [(1,), (2,), (3,)],
    )
    def test_recover_posterior(self, ndim):
        """Test if pathfinder is able to estimate well enough the posterior of a
        normal-normal conjugate model"""

        def logp_posterior_conjugate_normal_model(
            x, observed, prior_mu, prior_prec, true_prec
        ):
            n = observed.shape[0]
            posterior_cov = jnp.linalg.inv(prior_prec + n * true_prec)
            posterior_mu = (
                posterior_cov
                @ (
                    prior_prec @ prior_mu[:, None]
                    + n * true_prec @ observed.mean(0)[:, None]
                )
            )[:, 0]
            return stats.multivariate_normal.logpdf(x, posterior_mu, posterior_cov)

        def logp_unnormalized_posterior(x, observed, prior_mu, prior_prec, true_cov):
            logp = 0.0
            logp += stats.multivariate_normal.logpdf(x, prior_mu, prior_prec)
            logp += stats.multivariate_normal.logpdf(observed, x, true_cov).sum()
            return logp

        rng_key_chol, rng_key_observed, rng_key_pathfinder = jax.random.split(
            jax.random.PRNGKey(1), 3
        )

        L = jnp.tril(jax.random.normal(rng_key_chol, (ndim, ndim)))
        true_mu = jnp.arange(ndim)
        true_cov = L @ L.T
        true_prec = jnp.linalg.pinv(true_cov)

        prior_mu = jnp.zeros(ndim)
        prior_prec = prior_cov = jnp.eye(ndim)

        observed = jax.random.multivariate_normal(
            rng_key_observed, true_mu, true_cov, shape=(1_000,)
        )

        logp_model = functools.partial(
            logp_unnormalized_posterior,
            observed=observed,
            prior_mu=prior_mu,
            prior_prec=prior_prec,
            true_cov=true_cov,
        )

        x0 = jnp.ones(ndim)
        kernel = pathfinder(rng_key_pathfinder, logp_model)
        out = self.variant(kernel.init)(x0)

        sim_p, log_p = lbfgs_sample(
            rng_key_pathfinder,
            10_000,
            out.position,
            out.grad_position,
            out.alpha,
            out.beta,
            out.gamma,
        )

        log_q = logp_posterior_conjugate_normal_model(
            sim_p, observed, prior_mu, prior_prec, true_prec
        )

        kl = (log_p - log_q).mean()
        self.assertAlmostEqual(kl, 0.0, delta=1e-3)


if __name__ == "__main__":
    absltest.main()
