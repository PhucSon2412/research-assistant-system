# Dense Adapter Stacks for Low-Resource Translation

Authors: Hana Mensah (fictional author; synthetic evaluation paper).
Identifier: doi:10.5555/ras-eval.p02 (fictional, not registered).

## Abstract

Dense Adapter Stacks (DAS) insert small bottleneck adapters into every layer of a frozen multilingual model.
DAS improves Kinyarwanda–English BLEU by 1.1 points while updating only 3% of the parameters.

## Introduction

Full fine-tuning of large multilingual models is expensive for low-resource language pairs.
Adapters keep the base model frozen and train a small number of new weights.

## Methods

The base model is a 12-layer multilingual encoder-decoder that stays frozen during training.
Each layer receives two adapters of rank 64, one after attention and one after the feed-forward block.
We trained on the same 1.2 million Kinyarwanda–English sentence pairs used in prior routing work.
Optimisation settings follow the base model release and are not tuned for this study.

## Results

DAS scored 26.6 BLEU, against 25.5 BLEU for the dense baseline, a gain of 1.1 points.
Training required 21 GPU-hours on the same hardware class as the baseline.
On code-switched input DAS improved by 0.8 BLEU over the baseline instead of degrading.

## Efficiency

The adapters add 3% trainable parameters and 5% inference latency at batch size 1.
Memory use during training was 40% lower than full fine-tuning.

## Limitations

The DAS gain shrinks to 0.2 BLEU when more than 5 million sentence pairs are available.
We report a single random seed per configuration.

## References

[1] L. Okafor and M. Reyes. Gated Sparse Routing for Low-Resource Machine Translation. 2026.
[2] J. Pereira. Parameter-Efficient Transfer for Translation. 2023.
