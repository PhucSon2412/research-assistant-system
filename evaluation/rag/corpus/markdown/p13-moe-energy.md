# Energy Cost of Mixture-of-Experts Inference on Edge Devices

Authors: Min-jun Seo and Clara Weiss (fictional authors; synthetic evaluation paper).
Identifier: doi:10.5555/ras-eval.p13 (fictional, not registered).

## Abstract

We measure the energy of sparse mixture-of-experts translation inference on a single-board computer.

## Methods

All measurements used a single-board computer with 8 GB of RAM and an external power meter sampling at 1 kHz.
The sparse model routes each token to two experts, and the dense model has the same number of active parameters.

## Results

The top-2 mixture-of-experts model used 1.3 times the energy per token of the dense model.
Loading expert weights from flash memory accounted for 58% of the extra energy.
Caching the four most used experts in RAM reduced the overhead to 1.1 times the dense energy per token.

## Limitations

We measured only one device class and only batch size 1.
