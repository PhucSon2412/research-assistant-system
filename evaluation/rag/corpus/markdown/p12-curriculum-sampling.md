# Curriculum Sampling for Low-Resource Translation

Authors: Adaeze Nwosu (fictional author; synthetic evaluation paper).
Identifier: doi:10.5555/ras-eval.p12 (fictional, not registered).

## Abstract

We order training batches from short, frequent sentences to long sentences with rare words.
Curriculum sampling improves Kinyarwanda–English BLEU by 1.7 points and Swahili–English BLEU by 0.9 points.

## Methods

Sentences are scored by length and by the mean log-frequency of their tokens.
The curriculum spans the first 40% of training steps, after which batches are sampled uniformly.
We trained on 1.2 million Kinyarwanda–English pairs and 2.0 million Swahili–English pairs.

## Results

The curriculum model reached 27.2 BLEU on Kinyarwanda–English against a baseline of 25.5 BLEU.
It reached convergence 18% sooner in wall-clock time than uniform sampling.
On Swahili–English, BLEU improved from 31.0 to 31.9.

## Limitations

Curriculum sampling had no measurable effect on code-switched input, with a change of 0.1 BLEU.
It requires a frequency table computed from the full training set before training starts.

## References

[1] L. Okafor and M. Reyes. Gated Sparse Routing for Low-Resource Machine Translation. 2026.
