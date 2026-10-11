### lab-ucayali-4mo (lab latency_ms=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 1 | 0 | 43.6 [43.6-43.6] | 1.00x | 5.60 | 1244 | 1994.7 | 10844 | 62.9 | 11.5 | reference |

### lab-ucayali-4mo (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| auto | 1 | 1 | 9.2 [9.2-9.2] | n/a | 26.64 | 1160 | 1740.0 | 2928 | 25.2 | 4.0 | bit-exact |
| auto-mem2600 | 1 | 0 | 18.5 [18.5-18.5] | n/a | 13.22 | 1160 | 1740.0 | 2237 | 28.8 | 4.4 | bit-exact |

### lab-ucayali-x2

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 0 | 6 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | no successful run |
| fixed-4 | 9 | 1 | 7.0 [5.8-13.7] | n/a | 8.68 | 580 | 870.0 | 2292 | 10.3 | 7.0 | bit-exact |
| fixed-16 | 36 | 1 | 4.5 [4.3-5.5] | n/a | 13.58 | 590 | 887.5 | 2703 | 11.0 | 4.5 | bit-exact |
| auto | 15 | 3 | 5.3 [4.4-16.1] | n/a | 11.50 | 580 | 870.0 | 3016 | 13.4 | 5.3 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 18.1 [18.1-18.2] | 1.00x | 3.36 | 622 | 997.4 | 16865 | 28.7 | 18.1 | reference |
| fixed-4 | 3 | 0 | 3.4 [3.4-3.4] | 5.35x | 17.99 | 580 | 870.0 | 3788 | 13.1 | 3.4 | bit-exact |
| fixed-16 | 3 | 0 | 2.3 [2.3-2.3] | 7.92x | 26.64 | 580 | 870.0 | 4340 | 14.0 | 2.3 | bit-exact |
| fixed-32 | 3 | 0 | 2.5 [2.4-4.0] | 7.17x | 24.12 | 585 | 871.8 | 5145 | 14.3 | 2.5 | bit-exact |
| auto-max32 | 3 | 0 | 2.3 [2.3-2.3] | 7.91x | 26.61 | 580 | 870.0 | 4787 | 14.1 | 2.3 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| auto | 1 | 2 | 14.0 [14.0-14.0] | n/a | 4.37 | 580 | 870.0 | 2420 | 12.0 | 14.0 | bit-exact |
| auto-mem2600 | 2 | 0 | 11.9 [9.9-13.9] | n/a | 5.80 | 580 | 870.0 | 2496 | 12.9 | 11.9 | bit-exact |
| auto-mem3000 | 1 | 0 | 5.7 [5.7-5.7] | n/a | 10.76 | 580 | 870.0 | 2071 | 10.1 | 5.7 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0,max_inflight=2)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| fixed-4 | 2 | 0 | 10.8 [10.1-11.5] | n/a | 5.75 | 1672 | 1367.7 | 3461 | 14.1 | 10.8 | bit-exact |
| fixed-16 | 2 | 0 | 12.1 [11.9-12.3] | n/a | 5.04 | 1726 | 1346.1 | 3833 | 14.4 | 12.1 | bit-exact |
| auto | 2 | 0 | 9.2 [9.0-9.4] | n/a | 6.62 | 1730 | 1337.3 | 3858 | 14.4 | 9.2 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0,max_inflight=4)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 23.5 [23.3-23.6] | 1.00x | 2.60 | 1218 | 1389.8 | 16827 | 30.6 | 23.5 | reference |
| fixed-4 | 2 | 0 | 8.6 [8.6-8.7] | 2.72x | 7.08 | 1545 | 1696.4 | 3660 | 14.5 | 8.6 | bit-exact |
| fixed-16 | 2 | 0 | 8.3 [7.3-9.2] | 2.84x | 7.79 | 1614 | 1574.6 | 3966 | 14.3 | 8.2 | bit-exact |
| auto | 2 | 0 | 8.7 [8.3-9.0] | 2.70x | 7.07 | 1620 | 1593.9 | 4110 | 14.2 | 8.6 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0.3)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| fixed-16 | 2 | 0 | 70.6 [67.1-74.1] | n/a | 0.87 | 1974 | 1250.7 | 3605 | 15.3 | 70.6 | bit-exact |
| auto | 2 | 0 | 69.2 [67.2-71.3] | n/a | 0.88 | 1929 | 1229.4 | 3561 | 15.6 | 69.2 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=20,bandwidth_mbps=10,fail_rate=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 115.1 [115.1-115.2] | 1.00x | 0.53 | 622 | 997.4 | 16724 | 32.9 | 115.1 | reference |
| fixed-4 | 2 | 0 | 89.3 [89.2-89.3] | 1.29x | 0.68 | 580 | 870.0 | 3245 | 16.8 | 89.3 | bit-exact |
| fixed-16 | 2 | 0 | 88.3 [88.3-88.4] | 1.30x | 0.69 | 580 | 870.0 | 3863 | 17.0 | 88.3 | bit-exact |
| auto-max32 | 2 | 0 | 88.2 [88.2-88.2] | 1.31x | 0.69 | 580 | 870.0 | 4021 | 17.7 | 88.2 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=50,bandwidth_mbps=0,fail_rate=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 28.0 [27.9-28.0] | 1.00x | 2.18 | 622 | 997.4 | 16947 | 28.8 | 28.0 | reference |
| fixed-4 | 5 | 0 | 12.0 [12.0-13.6] | 2.33x | 5.08 | 580 | 870.0 | 3544 | 13.7 | 12.0 | bit-exact |
| fixed-16 | 3 | 0 | 4.3 [4.3-4.3] | 6.50x | 14.17 | 580 | 870.0 | 3839 | 13.3 | 4.3 | bit-exact |
| fixed-32 | 3 | 0 | 3.3 [3.2-3.3] | 8.58x | 18.72 | 580 | 870.0 | 4420 | 13.7 | 3.2 | bit-exact |
| auto | 9 | 1 | 7.9 [7.8-8.0] | 3.56x | 7.76 | 580 | 870.0 | 3060 | 11.5 | 7.9 | bit-exact |
| auto-max32 | 3 | 0 | 4.3 [4.2-4.4] | 6.46x | 14.08 | 580 | 870.0 | 4080 | 13.3 | 4.3 | bit-exact |

### lab-ucayali-x2 (lab latency_ms=50,bandwidth_mbps=0,fail_rate=0,max_inflight=12)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 29.6 [29.4-29.8] | 1.00x | 2.06 | 726 | 1053.0 | 16815 | 28.7 | 29.6 | reference |
| fixed-4 | 3 | 0 | 16.8 [16.7-16.8] | 1.76x | 3.63 | 717 | 1010.5 | 3593 | 14.0 | 16.8 | bit-exact |
| fixed-16 | 3 | 0 | 18.3 [18.2-18.5] | 1.62x | 3.34 | 1066 | 1016.3 | 3907 | 14.2 | 18.2 | bit-exact |
| fixed-32 | 3 | 0 | 18.0 [17.4-18.0] | 1.65x | 3.40 | 1028 | 1026.4 | 4002 | 14.3 | 17.9 | bit-exact |
| auto | 3 | 0 | 18.9 [17.7-20.3] | 1.57x | 3.24 | 1215 | 1061.8 | 4138 | 14.7 | 18.8 | bit-exact |
| auto-max32 | 3 | 0 | 18.2 [18.1-18.7] | 1.62x | 3.35 | 1185 | 1039.1 | 4168 | 14.5 | 18.2 | bit-exact |

### lab-ucayali-x2-5km

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 1.0 [0.9-1.0] | 1.00x | 2.08 | 340 | 99.5 | 733 | 1.7 | 1.0 | reference |
| fixed-16 | 3 | 0 | 0.3 [0.3-0.3] | 2.91x | 6.03 | 276 | 86.2 | 332 | 1.2 | 0.3 | bit-exact |
| auto | 5 | 0 | 0.3 [0.3-0.3] | 2.87x | 5.95 | 276 | 86.2 | 317 | 1.2 | 0.3 | bit-exact |

### lab-ucayali-x2-5km (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0,max_inflight=4)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 1.7 [1.7-1.8] | 1.00x | 1.16 | 347 | 102.0 | 741 | 1.8 | 1.7 | reference |
| fixed-16 | 2 | 0 | 4.0 [3.7-4.2] | 0.43x | 0.51 | 318 | 89.2 | 292 | 1.2 | 4.0 | bit-exact |
| auto | 3 | 0 | 4.5 [3.9-5.0] | 0.38x | 0.44 | 311 | 87.8 | 292 | 1.2 | 4.5 | bit-exact |

### lab-ucayali-x2-5km (lab latency_ms=0,bandwidth_mbps=0,fail_rate=0.05)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 8.9 [8.4-9.4] | 1.00x | 0.23 | 382 | 104.9 | 735 | 1.8 | 8.9 | reference |
| fixed-16 | 2 | 0 | 3.0 [2.6-3.4] | 2.98x | 0.71 | 299 | 88.7 | 312 | 1.2 | 3.0 | bit-exact |
| auto | 3 | 0 | 3.6 [2.9-4.1] | 2.50x | 0.56 | 302 | 90.8 | 304 | 1.2 | 3.6 | bit-exact |

### lab-ucayali-x2-5km (lab latency_ms=0,bandwidth_mbps=2,fail_rate=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 50.2 [50.2-50.2] | 1.00x | 0.04 | 340 | 99.5 | 743 | 2.5 | 50.2 | reference |
| fixed-16 | 2 | 0 | 43.2 [43.2-43.2] | 1.16x | 0.05 | 276 | 86.2 | 325 | 1.8 | 43.2 | bit-exact |
| auto | 2 | 0 | 43.2 [43.2-43.2] | 1.16x | 0.05 | 276 | 86.2 | 330 | 1.8 | 43.2 | bit-exact |

### lab-ucayali-x2-5km (lab latency_ms=300,bandwidth_mbps=0,fail_rate=0)

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 2 | 0 | 27.6 [27.6-27.6] | 1.00x | 0.07 | 340 | 99.5 | 742 | 2.4 | 27.6 | reference |
| fixed-16 | 2 | 0 | 5.8 [5.8-5.8] | 4.79x | 0.34 | 276 | 86.2 | 309 | 1.4 | 5.8 | bit-exact |
| auto | 4 | 0 | 5.8 [5.8-5.8] | 4.76x | 0.34 | 276 | 86.2 | 305 | 1.5 | 5.8 | bit-exact |

### lakemead-1m

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 34.9 [34.1-35.0] | 1.00x | 1.50 | 240 | 369.4 | 12738 | 17.9 | 34.9 | reference |
| fixed-4 | 3 | 0 | 20.3 [20.2-20.4] | 1.72x | 2.58 | 220 | 284.1 | 2501 | 7.5 | 20.3 | bit-exact |
| fixed-16 | 3 | 0 | 14.0 [13.7-14.1] | 2.50x | 3.74 | 220 | 284.1 | 2873 | 7.3 | 14.0 | bit-exact |
| auto-capped | 3 | 0 | 14.8 [13.4-15.9] | 2.37x | 3.55 | 220 | 284.1 | 2277 | 7.0 | 14.8 | bit-exact |
| auto-max32 | 3 | 0 | 14.8 [14.0-15.0] | 2.36x | 3.54 | 220 | 284.1 | 2959 | 8.2 | 14.8 | bit-exact |

### ucayali-1m

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 44.1 [42.8-44.5] | 1.00x | 1.38 | 311 | 498.7 | 10572 | 17.6 | 44.1 | reference |
| fixed-4 | 3 | 0 | 26.4 [26.1-28.7] | 1.67x | 2.31 | 290 | 435.0 | 2265 | 9.3 | 26.4 | bit-exact |
| fixed-16 | 6 | 0 | 20.9 [20.1-24.1] | 2.11x | 2.92 | 290 | 435.0 | 2647 | 9.5 | 20.8 | bit-exact |
| auto | 3 | 0 | 22.8 [20.3-36.2] | 1.94x | 2.68 | 290 | 435.0 | 2576 | 9.9 | 22.8 | bit-exact |
| auto-capped | 3 | 0 | 22.7 [22.1-24.5] | 1.94x | 2.69 | 290 | 435.0 | 2067 | 9.0 | 22.7 | bit-exact |
| auto-max32 | 3 | 0 | 21.5 [21.1-22.4] | 2.05x | 2.84 | 290 | 435.0 | 2713 | 9.8 | 21.4 | bit-exact |

### ucayali-small-3m

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 19.6 [19.0-19.8] | 1.00x | 0.30 | 253 | 70.3 | 392 | 3.0 | 7.7 | reference |
| fixed-16 | 6 | 0 | 4.2 [4.0-4.3] | 4.72x | 1.44 | 193 | 52.1 | 265 | 1.5 | 4.0 | bit-exact |
| auto | 3 | 0 | 4.3 [4.2-4.3] | 4.61x | 1.40 | 193 | 52.1 | 268 | 1.5 | 4.1 | bit-exact |
| auto-max32 | 3 | 0 | 5.0 [5.0-5.1] | 3.92x | 1.19 | 193 | 52.1 | 313 | 1.9 | 4.5 | bit-exact |

### ucayali-warp-1m

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 45.3 [44.9-47.5] | 1.00x | 1.40 | 311 | 498.7 | 10852 | 22.2 | 45.3 | reference |
| fixed-16 | 6 | 0 | 20.2 [18.8-22.1] | 2.24x | 3.14 | 311 | 498.7 | 2958 | 16.4 | 20.2 | bit-exact |
| auto | 3 | 0 | 22.5 [22.0-23.8] | 2.01x | 2.81 | 311 | 498.7 | 2924 | 16.6 | 22.5 | bit-exact |
| auto-max32 | 3 | 0 | 24.8 [23.1-32.3] | 1.83x | 2.56 | 311 | 498.7 | 2894 | 17.1 | 24.7 | bit-exact |

### yukon-sparse-3m

| config | n | failed | wall s median [IQR] | speed-up vs baseline | useful MB/s | GET+HEAD | downloaded MB | peak RSS MB | CPU s | first month s | correctness |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 0 | 145.4 [142.6-152.3] | 1.00x | 2.46 | 1031 | 1663.0 | 14484 | 55.1 | 63.4 | reference |
| fixed-4 | 3 | 0 | 69.2 [67.8-71.2] | 2.10x | 5.17 | 837 | 1288.6 | 3495 | 25.8 | 34.0 | bit-exact |
| fixed-16 | 3 | 0 | 48.4 [44.9-58.1] | 3.00x | 7.40 | 837 | 1288.6 | 4570 | 24.6 | 41.9 | bit-exact |
| auto-capped | 3 | 0 | 69.5 [59.7-101.2] | 2.09x | 5.15 | 837 | 1288.6 | 3890 | 23.7 | 47.3 | bit-exact |
| auto-max32 | 3 | 0 | 41.8 [41.5-44.3] | 3.48x | 8.56 | 837 | 1288.6 | 4617 | 25.0 | 37.9 | bit-exact |

