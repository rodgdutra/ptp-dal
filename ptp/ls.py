"""Least-squares Estimator
"""
import logging
import numpy as np
logger = logging.getLogger(__name__)


class Ls():
    def __init__(self, N, data, T_ns=float('inf')):
        """Least-squares Time Offset Estimator

        Args:
            N    : observation window length (number of measurements per window)
            data : Array of objects with simulation data
            T_ns : nominal time offset measurement period in nanoseconds, used
                   for **debugging only**. It is used to obtain the fractional
                   frequency offset y (drift in sec/sec) when using the
                   efficient LS implementation, since the latter only estimates
                   y*T_ns (drif in nanoseconds/measurement). In the end, this is
                   used for plotting the frequency offset.

        """
        self.N    = N
        self.data = data
        self.T_ns = T_ns

        if (np.isinf(T_ns)):
            logger.warning("Measurement period was not defined")

    def process(self, impl="eff"):
        """Process the observations

        Using the raw time offset offset measurements and the Sync arrival
        timestamps, estimate the time and frequency offset of windows of
        samples.

        There are three distinct implementations for least-squares: "t2", "t1"
        and "eff", which are described next:

        - "t2" (default) : Uses timestamp "t2" when forming the observation
          matrix H.
        - "t1"           : Uses timestamp "t1" when forming the observation
          matrix H.
        - "eff"          : Computational-efficient implementation

        NOTE: The ideal time samples to be used in this matrix would be the true
        values of timestamps "t2", according to the reference time, not the
        slave time. Hence, the problem with using timestamps "t2" directly is
        that they are subject to slave impairments. When observing a long
        window, the last timestamp "t2" in the window may have drifted
        substantially with respect to the true "t2". In contrast, timestamps
        "t1" are taken at the master side, so they are directly from the
        reference time. However, the disadvantage of using "t1" is that they do
        not reflect the actual Sync arrival time due to PDV. Finally, the
        "efficient" choice ignores PDV and any timescale innacuracies to favour
        implementation simplicity.

        Args:
            t_choice : Timestamp choice when assemblign obervation matrix

        """

        logger.info("Processing")

        n_data = len(self.data)
        N      = self.N

        # Vector of noisy time offset observations
        x_obs   = np.array([res["x_est"] for res in self.data])

        # Vector of master timestamps
        t1 = np.array([res["t1"] for res in self.data])

        # Learn the measurement period
        if (np.isinf(self.T_ns)):
            self.T_ns = float(np.mean(np.diff(t1)))
            logger.info("Automatically setting T_ns to %f ns", self.T_ns)

        # For "t1" and "t2", initialize vector of timestamps. For "eff",
        # initialize the matrix that is used for LS computations
        if (impl == "t1"):
            t = t1
        elif (impl == "t2"):
            t = [res["t2"] for res in self.data]
        elif (impl == "eff"):
            P = (2 / (N*(N+1))) * np.array([[(2*N - 1), -3], [-3, 6/(N-1)]]);
        else:
            raise ValueError("Unsupported LS timestamp mode")

        # Vectorized and efficient implementation
        #
        # NOTE: the non-vectorized but efficient implementation can still be
        # found below, as it helps for understanding. However, the following
        # vectorized implementation is the one that is effectively used, as it
        # is much faster.
        if (impl == "eff"):
            # Stack overlapping windows into columns of a matrix
            n_windows = n_data - N + 1
            X_obs     = np.zeros(shape=(N, n_windows))
            for i in range(N):
                X_obs[i, :] = x_obs[i:n_windows+i]

            # Q1 and Q2 accumulator values for each window
            Q      = np.zeros(shape=(2, n_windows))
            Q[0,:] = np.sum(X_obs, axis=0)
            Q[1,:] = np.dot(np.arange(N), X_obs)

            # LS estimations
            Theta        = np.dot(P,Q)
            X0           = Theta[0,:]
            Y_times_T_ns = Theta[1,:]
            Xf           = X0 + (Y_times_T_ns * (N-1))
            Y            = Y_times_T_ns / self.T_ns

            # Indices where results will be placed
            idx = np.arange(N-1, n_data)

            for i_x, i_res in enumerate(idx):
                self.data[i_res]["x_ls_" + impl] = Xf[i_x]
                self.data[i_res]["y_ls_" + impl] = Y[i_x]
                logger.debug("LS estimates\tx_f: %f ns y: %f ppb" %(
                    Xf[i_x], Y[i_x]*1e9))

            return

        # Iterate over sliding windows of observations
        for i in range(0, n_data - N + 1):
            # Window start and end indexes
            i_s = i
            i_e = i + N
            # Observation window
            x_obs_w = x_obs[i_s:i_e]

            # LS estimation
            if (impl == "eff"):
                # Accumulator 1
                if (i == 0):
                    Q_1   = np.sum(x_obs_w)
                else:
                    # Slide accumulator - throw away oldest and add new
                    Q_1 -= x_obs[i_s - 1]
                    Q_1 += x_obs[i_e -1]
                # Accumulator 2
                if (i == 0):
                    Q_2 = np.sum(np.multiply(np.arange(N), x_obs_w))
                else:
                    # See derivation in Igor Freire's thesis, Section 3.6
                    Q_2 -= Q_1
                    Q_2 += N * x_obs[i_e -1]
                # Accumulator vector
                Q     = np.array([Q_1, Q_2])
                # LS Estimation
                Theta        = np.dot(P,Q.T);
                x0           = Theta[0] # initial time offset within window
                y_times_T_ns = Theta[1] # drift in nanoseconds/measurement
                # Fit the final time offset within the current window
                x_f          = x0 + (y_times_T_ns * (N-1))
                # Fractional frequency offset
                y            = y_times_T_ns / self.T_ns
            else:
                # Timestamps over observation window
                t_w     = t[i_s:i_e]
                tau = np.asarray([float(tt - t_w[0]) for tt in t_w])
                # Observation matrix
                H   = np.hstack((np.ones((N, 1)), tau.reshape(N, 1)))
                # NOTE: the observation matrix has to be assembled every time
                # for this approach. The "efficient" approach does not need to
                # re-compute H (doesn't even use H)

                # LS estimation
                x0, y = np.linalg.lstsq(H, x_obs_w, rcond=None)[0]
                # LS-fitted final time offset within window
                T_obs = float(t_w[-1] - t_w[0])
                x_f   = x0 + y * T_obs

            # Include LS estimations within the simulation data
            self.data[i_e - 1]["x_ls_" + impl] = x_f
            self.data[i_e - 1]["y_ls_" + impl] = y

            logger.debug("LS estimates\tx_f: %f ns y: %f" %(
                x_f, y*1e9))

