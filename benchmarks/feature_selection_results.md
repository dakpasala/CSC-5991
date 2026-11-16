# Feature selection results: accuracy vs. router cost

Single-XGBoost feature ablation on the 10-second `no_applayer` dataset (19,468 sessions,
1,066,702 windows). Produced by `src/train_xgboost_ablation.py`; every number below comes
from `benchmarks/xgb_ablation_10s_no_applayer.csv` (70 models across three runs). Trained
models are in `XGBoostModels/10s_no_applayer/` on the Linux box (gitignored).

## Setup

- **Model:** one plain XGBoost classifier per feature set (`tree_method=hist`, `max_depth=8`,
  `learning_rate=0.1`, up to 2,000 rounds with early stopping after 50 on validation log loss).
  Not bagged: each row is a single model.
- **Splits, all by session (never by row):**
  - test: 212,184 windows, the same held-out sessions as the AutoGluon run (stratified,
    `test_frac=0.2`, `seed=0`), so results are directly comparable to its 0.98;
  - validation: 127,074 windows, 15% of the training sessions;
  - fit: 727,444 windows.
- **Decisions use validation only.** Early stopping, the importance ranking at each elimination
  step, and every recommendation are made on validation. Test accuracy is recorded for
  reporting and never used to choose anything.
- **Recursive feature elimination (RFE):** train, measure permutation importance on 20,000
  validation rows (3 shuffles), drop the weakest 20% of the remaining units, and repeat.
  Importance is recomputed each step because the features are highly redundant. The
  recommended step is the one with the fewest features within 0.001 of the run's best
  validation accuracy.

## How router cost is counted

- **Distribution family:** the six statistics of one per-window distribution, e.g. the
  `_min/_max/_mean/_median/_p25/_p75` of `down_payload_size`.
- **Full buffer:** min, max and mean can be updated one packet at a time in O(1) memory, but
  median, p25 and p75 need every value in the window kept. A family needs a full buffer if any
  of its median/p25/p75 columns is used, and one buffer serves all six statistics. Dropping
  `down_payload_size_mean` saves nothing while `down_payload_size_p25` is still used.
- **TCP sequence/RTT tracking:** `rtt_ms_*`, `*_retransmission_count` and
  `*_out_of_order_count` need per-flow sequence-number state for every TCP flow, kept for the
  whole session. `benchmark_feature_memory.go` measured this as the largest memory cost in the
  pipeline (about 8.9MB per session on average in Go). The "Full buffers" column does not
  include it, so it is listed separately.

## Candidate feature sets

| Option | Features | Distribution families | Full buffers | Needs TCP seq/RTT tracking | Val acc | Test acc | Test macro F1 |
|---|---|---|---|---|---|---|---|
| A: all features | 112 | 13 | 13 | yes | 0.9831 | 0.9827 | 0.9854 |
| B: no buffers, most accurate (`no_total_streaming`) | 51 | 9 | 0 | yes | 0.9815 | 0.9810 | 0.9836 |
| C: no buffers, recommended (streaming RFE step 4) | 22 | 9 | 0 | yes | 0.9808 | 0.9799 | 0.9824 |
| D: minimal (streaming RFE step 9) | 8 | 3 | 0 | no | 0.9714 | 0.9705 | 0.9721 |

Accuracy cost relative to all features: B -0.0016, C -0.0027,
D -0.0121 test accuracy.

**Recommended for a router: option C.** With 22 features and **0 full buffers**, it scored
**0.9799 test accuracy** (0.9824 macro F1), against 0.9827
for all 112 features. Every one of its features updates one packet at a time. It still uses
`rtt_ms_min`/`rtt_ms_max`, so it needs per-flow RTT tracking. Option D needs no sequence
tracking at all, which removes the largest memory cost, at about 1.2 points of accuracy. A
no-buffer set without RTT/retransmission features, somewhere between C and D, has not been
tested yet.

### Option C: 22 features, 0 full buffers, test 0.9799

- `frac_udp`
- `up_byte_count`
- `up_psh_count`
- `up_payload_size_max`
- `up_packet_size_max`
- `up_packet_size_mean`
- `up_recv_window_min`
- `up_recv_window_max`
- `up_interarrival_time_min`
- `up_interarrival_time_max`
- `down_byte_count`
- `down_ack_count`
- `down_psh_count`
- `down_payload_size_mean`
- `down_packet_size_min`
- `down_packet_size_mean`
- `down_recv_window_min`
- `down_recv_window_max`
- `down_recv_window_mean`
- `down_interarrival_time_min`
- `rtt_ms_min`
- `rtt_ms_max`

### Option D: 8 features, 0 full buffers, no sequence tracking, test 0.9705

- `frac_udp`
- `up_byte_count`
- `up_psh_count`
- `up_payload_size_max`
- `down_payload_size_mean`
- `down_recv_window_min`
- `down_recv_window_max`
- `down_recv_window_mean`

### Option B: 51 features, 0 full buffers, test 0.9810

- `num_flows`
- `num_new_flows`
- `num_continuing_flows`
- `frac_udp`
- `up_packet_count`
- `up_byte_count`
- `up_throughput_bps`
- `up_retransmission_count`
- `up_out_of_order_count`
- `up_syn_count`
- `up_ack_count`
- `up_rst_count`
- `up_psh_count`
- `up_urg_count`
- `up_payload_size_min`
- `up_payload_size_max`
- `up_payload_size_mean`
- `up_packet_size_min`
- `up_packet_size_max`
- `up_packet_size_mean`
- `up_recv_window_min`
- `up_recv_window_max`
- `up_recv_window_mean`
- `up_interarrival_time_min`
- `up_interarrival_time_max`
- `up_interarrival_time_mean`
- `down_packet_count`
- `down_byte_count`
- `down_throughput_bps`
- `down_retransmission_count`
- `down_out_of_order_count`
- `down_syn_count`
- `down_ack_count`
- `down_rst_count`
- `down_psh_count`
- `down_urg_count`
- `down_payload_size_min`
- `down_payload_size_max`
- `down_payload_size_mean`
- `down_packet_size_min`
- `down_packet_size_max`
- `down_packet_size_mean`
- `down_recv_window_min`
- `down_recv_window_max`
- `down_recv_window_mean`
- `down_interarrival_time_min`
- `down_interarrival_time_max`
- `down_interarrival_time_mean`
- `rtt_ms_min`
- `rtt_ms_max`
- `rtt_ms_mean`

## Fixed feature-set experiments

| Experiment | Features | Distribution families | Full buffers | Needs TCP seq/RTT tracking | Val acc | Test acc | Test macro F1 |
|---|---|---|---|---|---|---|---|
| `all` | 112 | 13 | 13 | yes | 0.9831 | 0.9827 | 0.9854 |
| `no_total` | 78 | 9 | 9 | yes | 0.9829 | 0.9824 | 0.9852 |
| `shape_only` | 87 | 9 | 9 | yes | 0.9751 | 0.9750 | 0.9779 |
| `infra_only` | 25 | 4 | 4 | yes | 0.9700 | 0.9705 | 0.9719 |
| `streaming_only` | 73 | 13 | 0 | yes | 0.9819 | 0.9811 | 0.9837 |
| `no_total_streaming` | 51 | 9 | 0 | yes | 0.9815 | 0.9810 | 0.9836 |

- `no_total`: drop all `total_*` columns. Each one is computed from the matching up/down
  values, so they are redundant by construction.
- `shape_only`: drop receive window, RTT and `frac_udp`, the server/CDN-fingerprint features
  that dominated the AutoGluon importance. Shows the services are still 97.5% separable from
  traffic shape alone.
- `infra_only`: only receive window, RTT and `frac_udp`.
- `streaming_only` / `no_total_streaming`: drop every median/p25/p75 column, leaving no full
  buffers. `total_*` adds nothing even here (0.9811
  vs. 0.9810).

## Train a final model on one option

Each option's feature list is in `benchmarks/feature_sets/` (`option_b.txt`, `option_c.txt`,
`option_d.txt`): one feature per line, `#` comments allowed. To train one XGBoost on exactly
those features (from the repo root, on the box with the dataset):

```bash
python src/train_xgboost_ablation.py --final benchmarks/feature_sets/option_c.txt
python src/train_xgboost_ablation.py --final benchmarks/feature_sets/option_c.txt --max-depth 6   # smaller model
```

Any custom list works the same way: write the feature names to a `.txt` file and pass it to
`--final`. The script runs in two stages. First it trains on the fit split with early stopping
on validation, which picks the number of rounds. Then it retrains on fit + validation with
proportionally more rounds, unless `--no-refit` is given. The final model is scored on test
once. It writes `XGBoostModels/10s_no_applayer/final_<name>.json` (the model),
`final_<name>.features.json` (the input column order the model expects) and `classes.json`,
and appends a `final_<name>` row to the results CSV.

## Elimination curves

### RFE from `no_total_streaming` (by feature, no buffers throughout)

Recommended step 4 (option C).

| Step | Features | Distribution families | Full buffers | Needs TCP seq/RTT tracking | Val acc | Test acc | Test macro F1 |
|---|---|---|---|---|---|---|---|
| 0 | 51 | 9 | 0 | yes | 0.9815 | 0.9810 | 0.9836 |
| 1 | 41 | 9 | 0 | yes | 0.9813 | 0.9809 | 0.9835 |
| 2 | 33 | 9 | 0 | yes | 0.9812 | 0.9807 | 0.9833 |
| 3 | 27 | 9 | 0 | yes | 0.9810 | 0.9803 | 0.9828 |
| 4 | 22 | 9 | 0 | yes | 0.9808 | 0.9799 | 0.9824 |
| 5 | 18 | 7 | 0 | yes | 0.9788 | 0.9784 | 0.9806 |
| 6 | 15 | 7 | 0 | yes | 0.9782 | 0.9770 | 0.9793 |
| 7 | 12 | 6 | 0 | yes | 0.9770 | 0.9763 | 0.9784 |
| 8 | 10 | 5 | 0 | yes | 0.9765 | 0.9754 | 0.9773 |
| 9 | 8 | 3 | 0 | no | 0.9714 | 0.9705 | 0.9721 |
| 10 | 7 | 3 | 0 | no | 0.9655 | 0.9644 | 0.9647 |
| 11 | 6 | 3 | 0 | no | 0.9621 | 0.9610 | 0.9605 |
| 12 | 5 | 3 | 0 | no | 0.9553 | 0.9548 | 0.9530 |
| 13 | 4 | 2 | 0 | no | 0.9454 | 0.9437 | 0.9403 |
| 14 | 3 | 2 | 0 | no | 0.9000 | 0.8970 | 0.8808 |

### RFE from `no_total` by family (one whole distribution family per unit)

Recommended step 8: 37 features, 6 buffers. Every step frees a real buffer.

| Step | Features | Distribution families | Full buffers | Needs TCP seq/RTT tracking | Val acc | Test acc | Test macro F1 |
|---|---|---|---|---|---|---|---|
| 0 | 78 | 9 | 9 | yes | 0.9829 | 0.9824 | 0.9852 |
| 1 | 72 | 9 | 9 | yes | 0.9829 | 0.9824 | 0.9851 |
| 2 | 67 | 9 | 9 | yes | 0.9830 | 0.9825 | 0.9852 |
| 3 | 63 | 9 | 9 | yes | 0.9829 | 0.9824 | 0.9851 |
| 4 | 60 | 9 | 9 | yes | 0.9828 | 0.9824 | 0.9851 |
| 5 | 57 | 9 | 9 | yes | 0.9828 | 0.9821 | 0.9849 |
| 6 | 55 | 9 | 9 | yes | 0.9826 | 0.9823 | 0.9850 |
| 7 | 43 | 7 | 7 | yes | 0.9822 | 0.9821 | 0.9849 |
| 8 | 37 | 6 | 6 | yes | 0.9820 | 0.9818 | 0.9845 |
| 9 | 31 | 5 | 5 | yes | 0.9805 | 0.9807 | 0.9835 |
| 10 | 25 | 4 | 4 | yes | 0.9784 | 0.9787 | 0.9812 |
| 11 | 24 | 4 | 4 | yes | 0.9783 | 0.9785 | 0.9810 |
| 12 | 18 | 3 | 3 | no | 0.9760 | 0.9757 | 0.9780 |

### RFE from `no_total` (by feature)

Recommended step 4: 33 features, 7 buffers.

| Step | Features | Distribution families | Full buffers | Needs TCP seq/RTT tracking | Val acc | Test acc | Test macro F1 |
|---|---|---|---|---|---|---|---|
| 0 | 78 | 9 | 9 | yes | 0.9829 | 0.9824 | 0.9852 |
| 1 | 63 | 9 | 9 | yes | 0.9829 | 0.9823 | 0.9851 |
| 2 | 51 | 9 | 9 | yes | 0.9828 | 0.9826 | 0.9853 |
| 3 | 41 | 9 | 8 | yes | 0.9825 | 0.9822 | 0.9849 |
| 4 | 33 | 9 | 7 | yes | 0.9820 | 0.9819 | 0.9846 |
| 5 | 27 | 9 | 7 | yes | 0.9815 | 0.9818 | 0.9845 |
| 6 | 22 | 9 | 5 | yes | 0.9804 | 0.9804 | 0.9830 |
| 7 | 18 | 8 | 4 | yes | 0.9799 | 0.9797 | 0.9822 |
| 8 | 15 | 8 | 3 | yes | 0.9787 | 0.9786 | 0.9811 |
| 9 | 12 | 6 | 3 | yes | 0.9782 | 0.9780 | 0.9801 |
| 10 | 10 | 5 | 3 | yes | 0.9738 | 0.9739 | 0.9760 |
| 11 | 8 | 4 | 2 | yes | 0.9706 | 0.9711 | 0.9727 |
| 12 | 7 | 3 | 2 | no | 0.9679 | 0.9682 | 0.9694 |
| 13 | 6 | 3 | 2 | no | 0.9646 | 0.9652 | 0.9661 |
| 14 | 5 | 3 | 2 | no | 0.9614 | 0.9619 | 0.9625 |
| 15 | 4 | 2 | 1 | no | 0.9547 | 0.9547 | 0.9545 |
| 16 | 3 | 2 | 1 | no | 0.9379 | 0.9370 | 0.9324 |

### RFE from all features (by feature)

Recommended step 4: 47 features, but still all 13 families (10 buffered). Counting features instead of buffers is what made this recommendation misleading for a router.

| Step | Features | Distribution families | Full buffers | Needs TCP seq/RTT tracking | Val acc | Test acc | Test macro F1 |
|---|---|---|---|---|---|---|---|
| 0 | 112 | 13 | 13 | yes | 0.9831 | 0.9827 | 0.9854 |
| 1 | 90 | 13 | 13 | yes | 0.9833 | 0.9826 | 0.9854 |
| 2 | 72 | 13 | 12 | yes | 0.9829 | 0.9825 | 0.9853 |
| 3 | 58 | 13 | 12 | yes | 0.9829 | 0.9824 | 0.9852 |
| 4 | 47 | 13 | 10 | yes | 0.9827 | 0.9822 | 0.9850 |
| 5 | 38 | 13 | 8 | yes | 0.9822 | 0.9822 | 0.9849 |
| 6 | 31 | 13 | 7 | yes | 0.9819 | 0.9820 | 0.9846 |
| 7 | 25 | 12 | 6 | yes | 0.9816 | 0.9815 | 0.9843 |
| 8 | 20 | 11 | 5 | yes | 0.9806 | 0.9804 | 0.9831 |
| 9 | 16 | 8 | 2 | yes | 0.9799 | 0.9795 | 0.9820 |
| 10 | 13 | 6 | 2 | yes | 0.9776 | 0.9774 | 0.9796 |
| 11 | 11 | 5 | 2 | yes | 0.9749 | 0.9744 | 0.9766 |
| 12 | 9 | 5 | 2 | yes | 0.9709 | 0.9705 | 0.9720 |
| 13 | 8 | 5 | 2 | yes | 0.9692 | 0.9693 | 0.9707 |
| 14 | 7 | 4 | 2 | yes | 0.9690 | 0.9687 | 0.9699 |
| 15 | 6 | 3 | 2 | no | 0.9660 | 0.9657 | 0.9665 |
| 16 | 5 | 3 | 2 | no | 0.9613 | 0.9609 | 0.9610 |
| 17 | 4 | 3 | 2 | no | 0.9585 | 0.9578 | 0.9576 |
| 18 | 3 | 2 | 2 | no | 0.9240 | 0.9235 | 0.9193 |

## Reproduce

From the repo root on the box that has `session_statistics_10_second_window_no_applayer.csv`:

```bash
python src/train_xgboost_ablation.py                                                  # run 1: fixed sets + RFE from all
python src/train_xgboost_ablation.py --experiments streaming_only,no_total_streaming   # run 2
python src/train_xgboost_ablation.py --experiments rfe --rfe-start no_total
python src/train_xgboost_ablation.py --experiments rfe --rfe-start no_total --rfe-unit family
python src/train_xgboost_ablation.py --experiments rfe --rfe-start no_total_streaming   # run 3
```
