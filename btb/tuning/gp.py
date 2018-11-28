from __future__ import division

import logging

import numpy as np
from scipy.stats import norm
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern

from btb.tuning.tuner import BaseTuner
from btb.tuning.uniform import Uniform
from btb.util import asarray2d

logger = logging.getLogger('btb')


class GP(BaseTuner):
    """GP tuner

    Args:
        r_minimum (int): the minimum number of past results this selector needs in order to use
            gaussian process for prediction. If not enough results are present during a ``fit``,
            subsequent calls to ``propose`` will revert to uniform selection.
    """

    def __init__(self, tunables, gridding=0, r_minimum=2):
        super(GP, self).__init__(tunables, gridding=gridding)
        self.r_minimum = r_minimum

    def fit(self, X, y):
        super(GP, self).fit(X, y)

        # skip training the process if there aren't enough samples
        if X.shape[0] < self.r_minimum:
            return

        X = asarray2d(X)
        y = asarray2d(y)

        self.gp = GaussianProcessRegressor(normalize_y=True)
        self.gp.fit(X, y)

    def predict(self, X):
        if self.X.shape[0] < self.r_minimum:
            # we probably don't have enough
            logger.info('Using Uniform sampler as user specified r_minimum '
                        'threshold is not met to start the GP based learning')
            return Uniform(self.tunables).predict(X)

        y, stdev = self.gp.predict(X, return_std=True)
        return np.hstack((asarray2d(y), asarray2d(stdev)))

    def _acquire(self, predictions):
        """
        Predictions from the GP will be in the form (prediction, error).
        The default acquisition function returns the index with the highest
        predicted value, not factoring in error.
        """
        return np.argmax(predictions[:, 0])


class GPEi(GP):
    """GPEi tuner

    The expected improvement criterion encodes a tradeoff between exploitation (points with high
    mean) and exploration (points with high uncertainty).

    See also::

        http://www.cs.toronto.edu/~kswersky/wp-content/uploads/nips2013transfer.pdf
        https://www.cse.wustl.edu/~garnett/cse515t/spring_2015/files/lecture_notes/12.pdf
    """

    @staticmethod
    def compute_ei(mu, sigma, t):
        """Compute expected improvement

        Args:
            mu (array-like): m x d array, where m is the number of candidates and d is the
                dimensionality of the hyperparameter space.
            sigma (array-like):
            t (scalar): best score so far

        """
        Phi = norm.cdf
        N = norm.pdf

        # because we are maximizing the scores, we do mu-y_best rather than the inverse, as is
        # shown in most reference materials
        z = (mu - t) / sigma

        ei = sigma * (z * Phi(z) + N(z))
        return ei

    def _acquire(self, predictions):
        # if ``predictions`` is not an array with shape (n, 2), then we must have been using the
        # Uniform tuner because of insufficient data. In this case, we defer to the default GP
        # acquisition function, which is to return the max prediction.
        if predictions.ndim <= 1 or (predictions.ndim == 2 and predictions.shape[1] == 1):
            return super(GPEi, self)._acquire(predictions)

        mu, sigma = predictions.T
        y_best = np.max(self.y)
        ei = self.compute_ei(mu, sigma, y_best)
        return np.argmax(ei)


class GPMatern52Ei(GPEi):
    """GPEi tuner with Matern 5/2 kernel

    See also::

        Snoek, Jasper, Hugo Larochelle, and Ryan P. Adams. "Practical bayesian optimization of
        machine learning algorithms." Advances in neural information processing systems. 2012.
    """

    def __init__(self, *args, **kwargs):
        super(GPMatern52Ei, self).__init__(*args, **kwargs)
        kernel = Matern(nu=5/2)
        self.gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True)


class GPEiVelocity(GPEi):
    """GCPEiVelocity tuner"""

    MULTIPLIER = -100   # magic number; modify with care
    N_BEST_Y = 5        # number of top values w/w to compute velocity

    def fit(self, X, y):
        """
        Train a gaussian process like normal, then compute a "Probability Of
        Uniform selection" (POU) value.
        """
        # first, train a gaussian process like normal
        super(GPEiVelocity, self).fit(X, y)

        # probability of uniform
        self.POU = 0
        if len(y) >= self.r_minimum:
            # get the best few scores so far, and compute the average distance
            # between them.
            top_y = sorted(y)[-self.N_BEST_Y:]
            velocities = [top_y[i + 1] - top_y[i] for i in range(len(top_y) - 1)]

            # the probability of returning random parameters scales inversely with
            # the "velocity" of top scores.
            self.POU = np.exp(self.MULTIPLIER * np.mean(velocities))

    def predict(self, X):
        """
        Use the POU value we computed in fit to choose randomly between GPEi and
        uniform random selection.
        """
        if np.random.random() < self.POU:
            # choose params at random to avoid local minima
            return Uniform(self.tunables).predict(X)

        return super(GPEiVelocity, self).predict(X)
