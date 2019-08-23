import argparse, logging, sys
import ptp.reader
import ptp.ls
import ptp.metrics
import ptp.pktselection
import ptp.kalman
import ptp.frequency
import ptp.window


def main():
    parser = argparse.ArgumentParser(description="PTP log reader test")
    parser.add_argument('-f', '--file',
                        default="log.json",
                        help='JSON log file.')
    parser.add_argument('--no-optimizer',
                        default=False,
                        action='store_true',
                        help='Whether or not to optimize window length')
    parser.add_argument('--use-secs',
                        default=False,
                        action='store_true',
                        help="Use secs that were actually captured " +
                        "(i.e. do not infer secs)")
    parser.add_argument('-N', '--num-iter',
                        default=0,
                        type=int,
                        help='Restrict number of iterations.')
    parser.add_argument('--verbose', '-v', action='count', default=1,
                        help="Verbosity (logging) level")
    args     = parser.parse_args()

    logging_level = 70 - (10 * args.verbose) if args.verbose > 0 else 0
    logging.basicConfig(stream=sys.stderr, level=logging_level)

    # Run PTP simulation
    reader = ptp.reader.Reader(args.file, infer_secs=(not args.use_secs),
                               reverse_ms=True)
    reader.run(args.num_iter)

    # Nominal message in nanoseconds
    if (reader.metadata is not None and "sync_period" in reader.metadata):
        T_ns = reader.metadata["sync_period"]*1e9
    else:
        T_ns = 1e9/4

    # Optimize window length configuration
    if (not args.no_optimizer):
        window_optimizer = ptp.window.Optimizer(reader.data, T_ns)
        window_optimizer.process('all', file=args.file)
        window_optimizer.save(args.file)
        est_op    = window_optimizer.est_op
        N_ls      = est_op["ls"]["N_best"]             # LS
        N_movavg  = est_op["sample-average"]["N_best"] # Moving average
        N_median  = est_op["sample-median"]["N_best"]  # Sample-median
        N_min     = est_op["sample-min"]["N_best"]     # Sample-minimum
        N_min_ls  = est_op["sample-min-ls"]["N_best"]  # Sample-minimum with LS
        N_mode    = est_op["sample-mode"]["N_best"]    # Sample-minimum
        N_mode_ls = est_op["sample-mode-ls"]["N_best"] # Sample-mode with LS
        N_ewma    = est_op["ls"]["N_best"]             # EWMA window

        print("Tuned window lengths:")
        for i in est_op:
            print("%20s: %d" %(i, est_op[i]["N_best"]))
    else:
        N_ls      = 105
        N_movavg  = 16
        N_median  = 16
        N_min     = 16
        N_min_ls  = 16
        N_mode    = 16
        N_mode_ls = 16
        N_ewma    = 16

    # Least-squares estimator
    ls = ptp.ls.Ls(N_ls, reader.data, T_ns)
    ls.process("eff")

    # Raw frequency estimations (differentiation of time offset measurements)
    freq_delta = 64
    freq_estimator = ptp.frequency.Estimator(reader.data, delta=freq_delta)
    freq_estimator.set_truth(delta=freq_delta)
    freq_estimator.optimize()
    freq_estimator.process()

    # Estimate time offset drifts due to frequency offset
    freq_estimator.estimate_drift()

    # Kalman
    # kalman = ptp.kalman.Kalman(reader.data, T_ns/1e9)
    kalman = ptp.kalman.Kalman(reader.data, T_ns/1e9,
                               trans_cov = [[1, 0], [0, 1e-2]],
                               obs_cov = [[1e4, 0], [0, 1e2]])
    kalman.process()

    # Moving average
    pkts = ptp.pktselection.PktSelection(N_movavg, reader.data)
    pkts.process("average", avg_impl="recursive")

    # Sample-median
    pkts.set_window_len(N_median)
    pkts.process("median")

    # Sample-minimum
    pkts.set_window_len(N_min)
    pkts.process("min")
    pkts.set_window_len(N_min_ls)
    pkts.process("min")

    # Exponentially weighted moving average
    pkts.set_window_len(N_ewma)
    pkts.process("ewma")

    # Sample-mode
    pkts.set_window_len(N_mode)
    pkts.process("mode")
    pkts.set_window_len(N_mode_ls)
    pkts.process("mode")

    # PTP analyser
    analyser = ptp.metrics.Analyser(reader.data)
    analyser.plot_toffset_vs_time()
    analyser.plot_foffset_vs_time()
    analyser.plot_temperature()
    analyser.plot_toffset_err_hist()
    analyser.plot_toffset_err_vs_time(show_raw = False)
    analyser.plot_foffset_err_hist()
    analyser.plot_foffset_err_vs_time()
    analyser.plot_delay_vs_time()
    analyser.plot_delay_vs_time(split=True)
    analyser.plot_delay_hist(n_bins=50)
    analyser.plot_delay_hist(split=True, n_bins=50)
    analyser.plot_delay_est_err_vs_time()
    analyser.plot_delay_asym_hist(n_bins=50)
    analyser.plot_delay_asym_vs_time()
    analyser.plot_pdv_vs_time()
    analyser.plot_pdv_hist()
    analyser.plot_toffset_diff_vs_time()
    analyser.plot_toffset_diff_hist()
    analyser.plot_mtie(show_raw = False)
    analyser.plot_max_te(show_raw=False, window_len = 1000)
    analyser.ptp_exchanges_per_sec()
    analyser.delay_asymmetry()
    analyser.toffset_err_stats()
    analyser.foffset_err_stats()

if __name__ == "__main__":
    main()


