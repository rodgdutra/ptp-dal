import logging
import numpy as np
from scipy import stats
logger = logging.getLogger(__name__)


SAMPLE_MODE_BIN_0 = 10 # starting value


class PktSelection():

    def __init__(self, N, data):
        """Packet Selection

        Args:
            N       : Observation window length
            data    : Array of objects with simulation data

        """
        self.N       = N
        self.data    = data
        self.i_batch = 0

        # Recursive moving-average
        self._movavg_accum    = 0              # recursive accumulator
        self._movavg_buffer   = np.zeros(2*N)  # circular buffer
        self._movavg_i        = N              # head index of the buffer

        # Exponentially-weight moving average
        self._ewma_alpha    = 1/N
        self._ewma_beta     = 1 - (1/N)
        self._ewma_last_avg = 0
        self._ewma_n        = 0 # sample index for bias correction

        # Sample-mode params
        self._sample_mode_bin    = SAMPLE_MODE_BIN_0 # used in window-by-window processing
        self._sample_mode_bin_fw = SAMPLE_MODE_BIN_0 # used in matrix-by-matrix processing
        self._sample_mode_bin_bw = SAMPLE_MODE_BIN_0 # used in matrix-by-matrix processing
        self._mode_stall_cnt     = 0

    def _window(self, v, N, shift=1, copy = False):
        """Split numpy vector into windows with configurable overlapping

        Args:
            v     : Numpy vector to split.
            N     : Target window length.
            shift : Controls the shift between consecutive windows or,
                    equivalently, the overlap. For instance, if shift=1, each
                    window overlaps with N-1 samples of the previous window.
            copy  : Set True to write to the array that is returned by this
                    function, as otherwise the returned array is a
                    memory-sharing view of the same numpy array (like the
                    result of a numpy.reshape).

        Returns:
            Matrix containing the overlapping windows. Each line will
            correspond to a window with N columns.

        """
        sh   = (v.size - N + 1, N)
        st   = v.strides * 2
        view = np.lib.stride_tricks.as_strided(v,
                                               strides = st,
                                               shape = sh)[0::shift]

        if (copy):
            return view.copy()
        else:
            return view

    def _sample_avg_normal(self, x_obs):
        """Calculate the average of a given time offset vector

        Args:
            x_obs   : Vector of time offset observations

        Returns:
            The average of the time offset vector

        """
        return np.mean(x_obs)

    def _sample_avg_normal_vec(self, x_obs):
        """Calculate the sample-average of time offset observation windows

        Args:
            x_obs   : Matrix with time offset observations windows

        Returns:
            Vector with the average of each individual time offset observation
            window

        """
        return np.mean(x_obs, axis=1)

    def _sample_avg_recursive(self, x_obs):
        """Calculate the average of a given time offset vector recursively

        Uses a circular (ring) buffer with size 2*N, where the tail pointer is
        always lagging the head pointer by N. The head pointer is used to save
        new values and the tail pointer is used to throw away the oldest value.

        Args:
            x_obs   : Scalar time offset observation

        Returns:
            The moving average

        """
        i_head                      = self._movavg_i
        i_tail                      = (i_head - self.N) % (2*self.N)
        # Put new observation on the head of the buffer
        self._movavg_buffer[i_head] = x_obs
        self._movavg_accum         += x_obs
        # Remove the oldest value on the tail of the buffer
        self._movavg_accum         -= self._movavg_buffer[i_tail]
        # Compute new average and advance head pointer
        new_avg                     = self._movavg_accum / self.N
        self._movavg_i              = (self._movavg_i + 1) % (2*self.N)

        return new_avg

    def _sample_avg_ewma(self, x_obs):
        """Calculate the exponentially weighted moving average (EWMA)

        Args:
            x_obs   : Scalar time offset observation

        Returns:
            The bias-corrected exponentially weighted moving average

        """
        new_avg             = (self._ewma_beta * self._ewma_last_avg) + \
                              self._ewma_alpha * x_obs
        # Save for the next iteration
        self._ewma_last_avg = new_avg
        # Apply bias correction (but don't save the bias-corrected average)
        self._ewma_n       += 1
        bias_corr           = 1 / (1 - (self._ewma_beta ** self._ewma_n))
        corr_avg            = new_avg * bias_corr

        return corr_avg

    def _sample_median(self, t2_minus_t1_w, t4_minus_t3_w):
        """Calculate the sample-median estimate of a given observation window

        Args:
            t2_minus_t1_w : Vector of (t2 - t1) differences
            t4_minus_t3_w : Vector of (t4 - t3) differences

        Returns:
            The time offset estimate based on sample-median

        """

        t2_minus_t1 = np.median(t2_minus_t1_w)
        t4_minus_t3 = np.median(t4_minus_t3_w)
        x_est       = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _sample_median_vec(self, t2_minus_t1_mtx, t4_minus_t3_mtx):
        """Calculate the sample-median estimates of given observation windows

        Args:
            t2_minus_t1_mtx : Matrix with windows (as lines) containing
                              (t2 - t1) differences
            t4_minus_t3_mtx : Matrix with windows (as lines) containing
                              (t4 - t3) differences

        Returns:
            The individual time offset estimates of each observation window
            based on sample-median

        """
        t2_minus_t1 = np.median(t2_minus_t1_mtx, axis=1)
        t4_minus_t3 = np.median(t4_minus_t3_mtx, axis=1)
        x_est       = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _eapf(self, t2_minus_t1_w, t4_minus_t3_w):
        """Compute the time offset based on earliest arrivals

        Apply an earliest arrival packet filter (EAPF) on both master-to-slave
        Sync messages and slave-to-master Delay_Req messages to compute time
        offset.

        Within a window of N message exchanges, find the earliest-arriving Sync
        and the earliest-arriving Delay_Req, that is, the ones that were subject
        to minimum delay. To find the Sync, observe the index where the
        difference (t2 - t1) was the minimum. To find the Delay_Req, look for
        the minimum of the difference (t4 - t3). In the end, use these minimum
        differences to compute the time offset estimate.

        Note that not necessarily the index of minimum (t2 - t1) must be the
        same as the index of minimum (t4 - t3), as long as the time offset
        remains sufficiently constant within the window.

        Args:
            t2_minus_t1_w : Vector of (t2 - t1) differences
            t4_minus_t3_w : Vector of (t4 - t3) differences

        Returns:
           The time offset estimate

        """
        t2_minus_t1 = np.amin(t2_minus_t1_w)
        t4_minus_t3 = np.amin(t4_minus_t3_w)
        x_est       = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _eapf_vec(self, t2_minus_t1_mtx, t4_minus_t3_mtx):
        """Compute the time offset based on earliest arrivals

        Vectorized version of the above (`_eapf`). Instead of processing a
        single observation window, this function processes a matrix with many
        (or all) observation windows.

        Args:
            t2_minus_t1_mtx : Matrix of (t2 - t1) differences
            t4_minus_t3_mtx : Matrix of (t4 - t3) differences

        Returns:
           Vector with the time offset estimates corresponding to each
           observation window

        """
        t2_minus_t1 = np.amin(t2_minus_t1_mtx, axis=1)
        t4_minus_t3 = np.amin(t4_minus_t3_mtx, axis=1)
        x_est       = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _sample_maximum(self, t2_minus_t1_w, t4_minus_t3_w):
        """Compute the time offset based on latest arrivals

        Implement the same logic of EAPF, but with the difference that the
        maximum delay is pursued rather than the minimum.

        Args:
            t2_minus_t1_w : Vector of (t2 - t1) differences
            t4_minus_t3_w : Vector of (t4 - t3) differences

        Returns:
           The time offset estimate

        """
        t2_minus_t1 = np.amax(t2_minus_t1_w)
        t4_minus_t3 = np.amax(t4_minus_t3_w)
        x_est       = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _sample_maximum_vec(self, t2_minus_t1_mtx, t4_minus_t3_mtx):
        """Compute the time offset based on latest arrivals

        Vectorized version of the above (`_sample_maximum`). Instead of
        processing a single observation window, this function processes a matrix
        with many (or all) observation windows.

        Args:
            t2_minus_t1_mtx : Matrix of (t2 - t1) differences
            t4_minus_t3_mtx : Matrix of (t4 - t3) differences

        Returns:
           Vector with the time offset estimates corresponding to each
           observation window

        """
        t2_minus_t1 = np.amax(t2_minus_t1_mtx, axis=1)
        t4_minus_t3 = np.amax(t4_minus_t3_mtx, axis=1)
        x_est       = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _sample_mode(self, t2_minus_t1_w, t4_minus_t3_w, cnt_threshold=3,
                     stall_patience=10):
        """Compute the time offset based on sample-mode

        Regarding the bin adjustment algorithm, note that a higher
        `cnt_threshold` will lead to more frequent adjustments (enlargements) of
        the bin width. A lower patience will also enlarge the bin width more
        frequently. Moreover, note that if the bin becomes too wide, the
        estimation resolution reduces. Hence, loosely speaking the
        `cnt_threshold` should be relatively small and `stall_patience`
        moderately higher.

        Args:
            t2_minus_t1_w  : Vector of (t2 - t1) differences
            t4_minus_t3_w  : Vector of (t4 - t3) differences
            cnt_threshold  : Minimum number of ocurrences on the mode bin
            stall_patience : Number of consecutive iterations of stalled mode
                             bin ocurrence count before the bin width is
                             adjusted

        Returns:
           The time offset estimate

        """
        bin_width      = self._sample_mode_bin
        half_bin_width = 0.5 * bin_width

        # Quantize timestamp difference vectors
        t2_minus_t1_q = np.round(t2_minus_t1_w / bin_width)
        t4_minus_t3_q = np.round(t4_minus_t3_w / bin_width)

        # Find the mode for (t2 - t1)
        (_, idx, counts) = np.unique(t2_minus_t1_q, return_index=True,
                                     return_counts=True)
        mode_cnt_fw      = np.amax(counts)
        mode_idx_fw      = idx[np.argmax(counts)]
        t2_minus_t1      = t2_minus_t1_q[mode_idx_fw] * bin_width + \
                           half_bin_width

        # Find the mode for (t4 - t3)
        (_, idx, counts) = np.unique(t4_minus_t3_q,
                                     return_index=True, return_counts=True)
        mode_cnt_bw      = np.amax(counts)
        mode_idx_bw      = idx[np.argmax(counts)]
        t4_minus_t3      = t4_minus_t3_q[mode_idx_bw] * bin_width + \
                           half_bin_width

        # Detect when we can't find a mode
        #
        # In case the occurrence count of the mode bin is below a threshold for
        # some consecutive number of iterations (denominated patience), we infer
        # that the mode detection is stalled and increase the bin width.
        if (mode_cnt_fw < cnt_threshold and mode_cnt_bw < cnt_threshold):
            self._mode_stall_cnt += 1
        else:
            self._mode_stall_cnt = 0

        if (self._mode_stall_cnt > stall_patience):
            self._sample_mode_bin += 10
            self._mode_stall_cnt   = 0

        # Final estimate
        x_est = (t2_minus_t1 - t4_minus_t3)/2

        return x_est

    def _sample_mode_vec(self, t2_minus_t1_mtx, t4_minus_t3_mtx,
                         cnt_threshold=3, n_tuning=100):
        """Compute the time offset based on sample-mode

        Vectorized sample-mode implementation. Its result differs from the
        non-vectorized sample-mode implementation due to how the bin adjustment
        is implemented.

        Args:
            t2_minus_t1_mtx : Matrix of (t2 - t1) differences
            t4_minus_t3_mtx : Matrix of (t4 - t3) differences
            cnt_threshold   : Minimum number of ocurrences on the mode bin
            n_tuning        : Number of realizations used for tuning

        Returns:
           Vector with the time offset estimates corresponding to each
           observation window

        """
        # Bin widths for t2-t1 and t4-t3 are independent in this implementation
        bin_width_fw = self._sample_mode_bin_fw
        bin_width_bw = self._sample_mode_bin_bw

        # Tune bin width
        #
        # If processing in batch mode, tune bin only for the first
        # batch. Otherwise, sample-mode would be quite slow.
        if (self.i_batch == 0):
            # Tune the bin width based on the first 100 observation windows
            done = False
            while (not done):
                # Quantize timestamp difference matrices
                t2_minus_t1_q = np.round(t2_minus_t1_mtx[:n_tuning,:] / bin_width_fw)
                t4_minus_t3_q = np.round(t4_minus_t3_mtx[:n_tuning,:] / bin_width_bw)

                # Find the mode
                t2_minus_t1_mode, mode_cnt_fw = stats.mode(t2_minus_t1_q, axis=1)
                t4_minus_t3_mode, mode_cnt_bw = stats.mode(t4_minus_t3_q, axis=1)

                # Adjust the bin such that less than 10% of the realizations (of
                # the windows) have a mode count below threshold. That is, we
                # want the mode to be significant, rather than close to a tie.
                done = True
                if ((mode_cnt_fw < cnt_threshold).sum() > int(0.1*n_tuning)):
                    bin_width_fw += 10
                    done = False

                if ((mode_cnt_bw < cnt_threshold).sum() > int(0.1*n_tuning)):
                    bin_width_bw += 10
                    done = False

            # Save tuned width
            self._sample_mode_bin_fw = bin_width_fw
            self._sample_mode_bin_bw = bin_width_bw

            logger.info("t2-t1 bin was adjusted to %d" %(bin_width_fw))
            logger.info("t4-t3 bin was adjusted to %d" %(bin_width_bw))

        half_bin_width_fw = 0.5 * bin_width_fw
        half_bin_width_bw = 0.5 * bin_width_bw

        # Quantize timestamp difference matrices
        t2_minus_t1_q = np.round(t2_minus_t1_mtx / bin_width_fw)
        t4_minus_t3_q = np.round(t4_minus_t3_mtx / bin_width_bw)

        # Find the mode
        t2_minus_t1_mode, mode_cnt_fw = stats.mode(t2_minus_t1_q, axis=1)
        t4_minus_t3_mode, mode_cnt_bw = stats.mode(t4_minus_t3_q, axis=1)

        # Descale to revert quantization
        t2_minus_t1 = t2_minus_t1_mode * bin_width_fw + half_bin_width_fw
        t4_minus_t3 = t4_minus_t3_mode * bin_width_bw + half_bin_width_bw

        # Final estimates
        x_est = (t2_minus_t1 - t4_minus_t3)/2

        return x_est.reshape(x_est.size)

    def set_window_len(self, N):
        """Change the window length

        Args:
            N : new window length N

        """
        self.N                   = N
        # Reset internal variables (some depend on N)
        self.i_batch             = 0
        self._movavg_accum       = 0
        self._movavg_buffer      = np.zeros(2*N)
        self._movavg_i           = N
        self._ewma_alpha         = 1/N
        self._ewma_beta          = 1 - (1/N)
        self._ewma_last_avg      = 0
        self._ewma_n             = 0
        self._sample_mode_bin    = SAMPLE_MODE_BIN_0
        self._sample_mode_bin_fw = SAMPLE_MODE_BIN_0
        self._sample_mode_bin_bw = SAMPLE_MODE_BIN_0
        self._mode_stall_cnt     = 0

    def _sample_by_sample(self, strategy, drift_comp, drift_est):
        """Sample-by-sample processing

        Args:
            strategy   : Selection strategy of interest (recursive moving
                         average or exponentially-weighted moving average)
            drift_comp : Whether to compensate drift of timestamp differences or
                         time offset measuremrents prior to computing packet
                         selection operators.
            drift_est  : Vector with time offset incremental drifts due to
                         frequency offset.

        """
        # Select vector of noisy time offset observations and delay measurements
        x_obs  = np.array([res["x_est"] for res in self.data])
        d_obs  = np.array([res["d_est"] for res in self.data])
        n_data = len(x_obs)

        # Assume that all elements of self.data contain "x_est" and "d_est"
        assert(len(x_obs) == len(self.data))
        assert(len(d_obs) == len(self.data))

        # Key for save data on global data records
        key = strategy.replace('-', '_')

        drift_accum = 0
        for i in range(0, n_data):
            if (drift_comp):
                # Accumulate drift continuously and try to average out
                # drift-removed time offset estimates
                drift_accum += drift_est[i]

            if (strategy == 'avg-recursive'):
                x_est = self._sample_avg_recursive(x_obs[i] - drift_accum)
            elif (strategy == 'ewma'):
                x_est = self._sample_avg_ewma(x_obs[i] - drift_accum)
            else:
                raise ValueError("Strategy choice %s unknown" %(strategy))

            # Re-add cumulative drift and save on global data records
            self.data[i][f"x_pkts_{key}"] = x_est + drift_accum

    def _window_by_window(self, strategy, drift_comp, drift_est):
        """Window-by-window processing

        Args:
            strategy   : Window-based packet selection strategy of interest
            drift_comp : Whether to compensate drift of timestamp differences or
                         time offset measuremrents prior to computing packet
                         selection operators.
            drift_est  : Vector with time offset incremental drifts due to
                         frequency offset.

        """
        for i in range(0, (len(self.data) - self.N) + 1):
            # Window start and end indexes
            i_s = i
            i_e = i + self.N

            # Observation window
            x_obs_w = np.array([res["x_est"] for res in self.data[i_s:i_e]])
            d_obs_w = np.array([res["d_est"] for res in self.data[i_s:i_e]])

            # Assume that all elements of the window contain "x_est" and "d_est"
            assert(len(x_obs_w) == len(d_obs_w))

            # Drift vector is the cumulative time offset drift due to
            # frequency offset relative to the beginning of the observation
            # window. Say if first sample of the window has an incremental
            # drift of 5 ns and second sample has a drift of 6 ns, the first
            # two elements of the drift vector below will be 5 and 11 ns.
            if (drift_comp):
                drift_w = drift_est[i_s:i_e].cumsum()

            # Operator that processes time offset measurement windows
            if (strategy == 'avg-normal'):
                # Remove drift from observed time offset
                if (drift_comp):
                    x_obs_w = x_obs_w - drift_w

                x_est = self._sample_avg_normal(x_obs_w)

            # Operators that process windows of timestamp differences
            else:
                t2_minus_t1_w = np.array([float(r["t2"] - r["t1"])
                                          for r in self.data[i_s:i_e]])
                t4_minus_t3_w = np.array([float(r["t4"] - r["t3"])
                                          for r in self.data[i_s:i_e]])

                # Remove drift from observed timestamp differences
                if (drift_comp):
                    t2_minus_t1_w = t2_minus_t1_w - drift_w
                    t4_minus_t3_w = t4_minus_t3_w + drift_w

                if (strategy == 'median'):
                    x_est = self._sample_median(t2_minus_t1_w,
                                                t4_minus_t3_w)

                elif (strategy == 'min'):
                    x_est = self._eapf(t2_minus_t1_w,
                                       t4_minus_t3_w)

                elif (strategy == 'max'):
                    x_est = self._sample_maximum(t2_minus_t1_w,
                                                 t4_minus_t3_w)

                elif (strategy == 'mode'):
                    x_est = self._sample_mode(t2_minus_t1_w,
                                              t4_minus_t3_w)
                else:
                    raise ValueError("Strategy choice %s unknown" %(strategy))

            # Re-add the drift that was accumulated during this observation
            # window to the estimate that was produced by the selection operator
            if (drift_comp):
                x_est = x_est + drift_w[-1]

            # Save on global data records
            key = strategy.replace('-', '_')
            self.data[i_e - 1][f"x_pkts_{key}"] = x_est

        if (strategy == 'mode' and
            (self._sample_mode_bin != SAMPLE_MODE_BIN_0)):
            logger.info("Sample-mode bin was increased to %d" %(
                self._sample_mode_bin))

    def _matrix_by_matrix(self, strategy, drift_comp, drift_est, batch,
                          batch_size):
        """Matrix-by-matrix processing

        This only processes window-based algorithms when vectorization is
        enabled. Each matrix consists in stacked windows. That is, each row
        corresponds to a distinct observation window and the number of columns
        correponds to the length of observation windows.

        Args:
            strategy   : Window-based packet selection strategy of interest
            drift_comp : Whether to compensate drift of timestamp differences or
                         time offset measuremrents prior to computing packet
                         selection operators.
            drift_est  : Vector with time offset incremental drifts due to
                         frequency offset.
            batch      : Whether to split dataset into batches.
            batch_size : Number of observation windows on each batch.

        """
        win_overlap = self.N - 1 # samples repeated on a window from past window
        new_per_win = self.N - win_overlap # new samples per window
        # NOTE: assume each window of size N has N-1 entries from the previous
        # window (i.e. fully overlapping windows)

        # Corresponding number of windows and batches
        n_windows  = int((len(self.data) - win_overlap)/new_per_win)
        n_batches  = np.ceil(n_windows/batch_size) if batch else 1
        batch_size = batch_size if batch else n_windows

        for i_w_s in range(0, n_windows, batch_size):
            i_w_e = i_w_s + batch_size # last window of this batch

            # Batch index
            self.i_batch = int(i_w_s / batch_size)
            logger.debug("Compute batch %d" %(self.i_batch))

            # Sample range covered by the windows
            i_s = i_w_s * new_per_win                       # 1st of the 1st window
            i_e = i_s + (batch_size-1)*new_per_win + self.N # last of the last

            # Calculate the drift windows
            drift_est_w = self._window(drift_est[i_s:i_e], self.N,
                                       shift=new_per_win)
            # NOTE: it is OK to let _window use copy = False here, i.e. it is OK if
            # the values change in the original drift_est vector.

            if (strategy == 'avg-normal'):
                # Time offset measurements:
                x_obs = np.array([res["x_est"] for res in self.data[i_s:i_e]])

                # Stacked windows with time offset measurements
                x_obs_w  = self._window(x_obs, self.N,
                                        shift=new_per_win,
                                        copy=True)
                # NOTE: copy is required because the values will changed/written
                # in place within the vectorized processing

                x_est = self._vectorized(strategy=strategy,
                                         drift_comp=drift_comp,
                                         drift_est=drift_est_w,
                                         x_obs=x_obs_w)
            else:
                # Timestamps differences
                t2_minus_t1   = np.array([float(r["t2"] - r["t1"])
                                          for r in self.data[i_s:i_e]])
                t4_minus_t3   = np.array([float(r["t4"] - r["t3"])
                                          for r in self.data[i_s:i_e]])

                # Form observation windows with timestamps differences
                t2_minus_t1_w = self._window(t2_minus_t1, self.N,
                                             shift=new_per_win,
                                             copy=True)
                t4_minus_t3_w = self._window(t4_minus_t3, self.N,
                                             shift=new_per_win,
                                             copy=True)
                # NOTE: copy is required because the values will be
                # changed/written in place within the vectorized processing

                # If batch processing is enabled, process each batch separately.
                # Otherwise, process all observation windows at once.
                x_est = self._vectorized(strategy=strategy,
                                         drift_comp=drift_comp,
                                         drift_est=drift_est_w,
                                         t2_minus_t1=t2_minus_t1_w,
                                         t4_minus_t3=t4_minus_t3_w)

            # There is one estimate per window of this batch, except if we don't
            # have windows to fill the entire batch
            assert(len(x_est) == batch_size or i_w_e > n_windows)

            # Save results on global data records
            key = strategy.replace('-', '_')
            for i in range(0, len(x_est)):
                first_idx_in_window = i_s + i*new_per_win
                last_idx_in_window  = first_idx_in_window + self.N - 1
                self.data[last_idx_in_window][f"x_pkts_{key}"] = x_est[i]

    def _vectorized(self, strategy, drift_comp, drift_est=None, \
                    x_obs=None, t2_minus_t1=None, t4_minus_t3=None):
        """Vectorized processing

        All observation windows are stacked as lines of a matrix

        Args:
            strategy    : Select the strategy of interest.
            drift_comp  : Whether to compensate drift of timestamp differences or
                          time offset measuremrents prior to computing packet
                          selection operators.
            drift_est   : Matrix with time offset incremental drifts due to
                          frequency offset.
            x_obs       : Matrix with time offset observations windows
            t2_minus_t1 : Matrix of (t2 - t1) differences
            t4_minus_t3 : Matrix of (t4 - t3) differences

        Returns:
            Vector with the time offset estimates corresponding to each
            observation window.

        """
        # Operator that processes time offset measurement windows
        if (strategy == 'avg-normal'):
            # Time offset measurement
            assert(x_obs.any()), "Time offset measurement not available"

            # Remove drift
            if (drift_comp):
                drift_cum_mtx  = drift_est.cumsum(axis=1)
                x_obs         -= drift_cum_mtx
                drift_offsets  = drift_cum_mtx[:,-1]

            # Sample-average operator
            x_est = self._sample_avg_normal_vec(x_obs)

        # Operators that process timestamp difference windows
        else:
            # Timestamp differences
            assert(t2_minus_t1.any()), "Timestamp differences not available"
            assert(t4_minus_t3.any()), "Timestamp differences not available"

            # Remove drift
            if (drift_comp):
                drift_cum_mtx  = drift_est.cumsum(axis=1)
                t2_minus_t1   -= drift_cum_mtx
                t4_minus_t3   += drift_cum_mtx
                drift_offsets  = drift_cum_mtx[:,-1]

            # Apply Selection operator
            if (strategy == 'median'):
                x_est = self._sample_median_vec(t2_minus_t1,
                                                t4_minus_t3)
            elif (strategy == 'min'):
                x_est = self._eapf_vec(t2_minus_t1,
                                       t4_minus_t3)
            elif (strategy == 'max'):
                x_est = self._sample_maximum_vec(t2_minus_t1,
                                                 t4_minus_t3)
            elif (strategy == 'mode'):
                #FIXME: Sample-mode need to be adjusted for batch processing.
                x_est = self._sample_mode_vec(t2_minus_t1,
                                              t4_minus_t3)
            else:
                raise ValueError("Strategy choice %s unknown" %(strategy))

        # Re-add "quasi-deterministic" drift to estimates
        if (drift_comp):
            x_est += drift_offsets

        return x_est

    def process(self, strategy, drift_comp=True, vectorize=True, batch=True, \
                batch_size=4096):
        """Process the observations

        Using the raw time offset measurements, estimate the time offset using
        sample-average ("avg-recursive" or "avg-normal"), EWMA ("ewma"),
        sample-median ("median"), sample-minimum ("min") or sample-mode ("mode")
        over sliding windows of observations.

        ----------------------------------------
        Drift compensation:

        If drift compensation is enabled, use vector of drifts to compensate the
        differences of timestamps along the observation window.

        Note that:
            x[n] = t2[n] - (t1[n] + dms[n])
            x[n] = (t3[n] + dsm[n]) - t4[n]

        so that

            t2[n] - t1[n] = +x[n] + dms[n],
            t4[n] - t3[n] = -x[n] + dsm[n]

        However, x[n] can be modeled as:

            x[n] = x_0 + drift[n],

        where the drift[n] term is mostly due to frequency offset, which
        sometimes can be accurately estimated (especially for high quality
        oscillators).

        Using this model, and if the drift is compensated, it can be stated
        that:

            t2[n] - t1[n] - drift[n] = +x_0 + dms[n],
            t4[n] - t3[n] + drift[n] = -x_0 + dsm[n]

        In the end, this means that drift-compensated timestamp differences will
        lead to an approximately constant time offset corrupted by variable
        delay realizations. This is the ideal input to packet selection
        operators. Once the operators process the drift-compensated timestamp
        differences, ideally its result would approach:

            x_est -> x_0

        To complete the work, the total (cumulative) drift over the observation
        window should be re-added to the estimate, so that it approaches the
        true time offset by the end of the observation window (while `x_0` is
        the offset in the beginning):

            x_est += drift[N-1]

        For the operators that process time offset observations x_est[n]
        directly, the drift can be similarly removed prior to the selection
        operator and the sum of the drift re-added to the final result.

        ----------------------------------------
        Vectorization and batch processing:

        There are two types of packet-selection processing in this function:
        sample-by-sample and window-by-window. All window-based processing
        algorithms also have corresponding vectorized implementations. The
        difference on the vectorized implementations is that, instead of
        processing one window at a time, they process several windows at once,
        i.e. process a matrix with windows stacked on top of each other. This is
        referred to as matrix-by-matrix processing.

        Depending on the dataset size, it may become infeasible to stack all
        available windows and process the resulting matrix at once. This could
        consume too much memory. To overcome this, this function relies also on
        batch processing. In this case, the vectorized implementation is still
        used, but the number of windows that are processed at once is
        limited. They are capped to the batch size.

        In contrast, the two methods (moving average methods) that are
        sample-by-sample cannot benefit from vectorization. The reason is that
        they are recursive, and hence must be computed sample by sample.

        Args:
            strategy   : Select the strategy of interest.
            drift_comp : Whether to compensate drift of timestamp differences or
                         time offset measuremrents prior to computing packet
                         selection operators.
            vectorize  : Whether to use vectorized implementation of selection
                         operators. When enabled, all observation windows will
                         be stacked as lines of a matrix and the full matrix
                         will be processed at once. Otherwise, each observation
                         window is processed independently.
            batch      : Whether to use batch processing with vectorized
                         implementation. When enabled, divided the matrix with
                         all the observation windows into batches. Use this
                         option in order to decrease the amount of RAM
                         required for processing all windows.
            batch_size : Define the size of each batch, that is, the number of
                         observation windows that will be processed at once.

        """
        if (drift_comp):
            drift_msg = " and drift compensation"
        else:
            drift_msg = ""

        logger.info("Processing sample-%s with N=%d" %(strategy, self.N) +
                    drift_msg)

        # Vector of time offset incremental drifts due to freq. offset:
        n_data    = len(self.data)
        drift_est = np.zeros(n_data)

        if (drift_comp):
            for i in range(0, n_data):
                if ("drift" in self.data[i]):
                    drift_est[i] = self.data[i]["drift"]

            assert(drift_est.any()), "Drift estimations not available"

        if (strategy == 'avg-recursive' or strategy == 'ewma'):
            # Sample-by-Sample processing
            self._sample_by_sample(strategy, drift_comp, drift_est)
        elif (vectorize):
            # Matrix-by-matrix processing
            self._matrix_by_matrix(strategy, drift_comp, drift_est, batch,
                                   batch_size)
        else:
            # Window-by-window processing
            self._window_by_window(strategy, drift_comp, drift_est)
