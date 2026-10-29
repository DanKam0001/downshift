| setup | pass | mean in | mean out | $/1k calls | diff vs ultra-think [95% CI] | within 5 pts | beats |
|---|---|---|---|---|---|---|---|
| ultra-think | 85.3% | 113 | 1198 | $3.705 | reference | - | - |
| ultra-off | 85.3% | 113 | 93 | $0.392 | +0.0 [-4.7, +4.7] | yes | no |
| super-off | 81.3% | 113 | 355 | $0.354 | -4.0 [-8.7, +0.7] | no | no |
| nano-off | 74.7% | 113 | 72 | $0.024 | -10.7 [-17.3, -4.0] | no | no |
| nano-think | 89.3% | 113 | 590 | $0.148 | +4.0 [+0.0, +8.7] | yes | no |
| lightning-off | 81.3% | 113 | 119 | $0.035 | -4.0 [-10.0, +2.0] | no | no |
| lightning-think | 87.3% | 113 | 1311 | $0.321 | +2.0 [-2.0, +6.0] | yes | no |
| nano-off+answer-only | 76.0% | 130 | 54 | $0.021 | -9.3 [-15.3, -3.3] | no | no |
| nano-off+long | 74.7% | 244 | 51 | $0.027 | -10.7 [-17.3, -4.0] | no | no |
| lightning-off+answer-only | 80.0% | 130 | 59 | $0.022 | -5.3 [-11.3, +0.7] | no | no |
| lightning-off+short | 82.7% | 80 | 125 | $0.035 | -2.7 [-8.7, +3.3] | no | no |
| lightning-off+long | 83.3% | 244 | 100 | $0.039 | -2.0 [-8.0, +4.0] | no | no |
| lightning-off+cap256 | 77.3% | 113 | 105 | $0.032 | -8.0 [-14.7, -1.3] | no | no |
| super-off+answer-only | 78.0% | 130 | 60 | $0.093 | -7.3 [-13.3, -1.3] | no | no |
| nano-think+answer-only | 90.7% | 130 | 477 | $0.122 | +5.3 [+1.3, +9.3] | yes | yes |
| nano-think+short | 87.3% | 80 | 610 | $0.151 | +2.0 [-3.3, +7.3] | yes | no |
| nano-think+long | 90.7% | 244 | 749 | $0.194 | +5.3 [+1.3, +10.0] | yes | yes |
| nano-think+total1024 | 80.7% | 113 | 448 | $0.114 | -4.7 [-10.0, +0.0] | no | no |
| nano-think+total2048 | 89.3% | 113 | 505 | $0.128 | +4.0 [+0.0, +8.0] | yes | no |
| nano-think+answer-only+total2048 | 88.7% | 130 | 418 | $0.108 | +3.3 [+0.0, +7.3] | yes | no |
| lightning-think+total2048 | 80.0% | 113 | 1029 | $0.254 | -5.3 [-10.7, -0.7] | no | no |

cheapest within 5 points: nano-think+answer-only+total2048 at $0.1082/1k vs $3.7053/1k = 34.3x cheaper
total spend: $1.0204
