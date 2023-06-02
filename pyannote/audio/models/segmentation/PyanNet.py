# MIT License
#
# Copyright (c) 2020 CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


from typing import Optional, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from pyannote.core.utils.generators import pairwise

from pyannote.audio.core.model import Model
from pyannote.audio.core.task import Task
from pyannote.audio.models.blocks.sincnet import SincNet
from pyannote.audio.utils.params import merge_dict


try:
    from transformers import WavLMModel

    TRANSFORMERS_IS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_IS_AVAILABLE = False


class PyanNetBase(Model):
    """PyanNet segmentation model

    Feature extractor (e.g. SincNet) > LSTM > Feed forward > Classifier

    Parameters
    ----------
    sample_rate : int, optional
        Audio sample rate. Defaults to 16kHz (16000).
    num_channels : int, optional
        Number of channels. Defaults to mono (1).
    sincnet : dict, optional
        Keyword arugments passed to the SincNet block.
        Defaults to {"stride": 1}.
    lstm : dict, optional
        Keyword arguments passed to the LSTM layer.
        Defaults to {"hidden_size": 128, "num_layers": 2, "bidirectional": True},
        i.e. two bidirectional layers with 128 units each.
        Set "monolithic" to False to split monolithic multi-layer LSTM into multiple mono-layer LSTMs.
        This may proove useful for probing LSTM internals.
    linear : dict, optional
        Keyword arugments used to initialize linear layers
        Defaults to {"hidden_size": 128, "num_layers": 2},
        i.e. two linear layers with 128 units each.
    """

    LSTM_DEFAULTS = {
        "hidden_size": 128,
        "num_layers": 2,
        "bidirectional": True,
        "monolithic": True,
        "dropout": 0.0,
    }
    LINEAR_DEFAULTS = {"hidden_size": 128, "num_layers": 2}

    def __init__(
        self,
        base_net: nn.Module,
        base_feature_dim: int,
        lstm: dict = None,
        linear: dict = None,
        sample_rate: int = 16000,
        num_channels: int = 1,
        task: Optional[Task] = None,
    ):
        super().__init__(sample_rate=sample_rate, num_channels=num_channels, task=task)

        lstm = merge_dict(self.LSTM_DEFAULTS, lstm)
        lstm["batch_first"] = True
        linear = merge_dict(self.LINEAR_DEFAULTS, linear)
        self.save_hyperparameters("lstm", "linear")

        # The name `sincnet` is preserved ONLY for backward compatibility with pre-trained weights
        self.sincnet = base_net

        if lstm["num_layers"] > 0:
            monolithic = lstm["monolithic"]
            if monolithic:
                multi_layer_lstm = dict(lstm)
                del multi_layer_lstm["monolithic"]
                self.lstm = nn.LSTM(base_feature_dim, **multi_layer_lstm)

            else:
                num_layers = lstm["num_layers"]
                if num_layers > 1:
                    self.dropout = nn.Dropout(p=lstm["dropout"])

                one_layer_lstm = dict(lstm)
                one_layer_lstm["num_layers"] = 1
                one_layer_lstm["dropout"] = 0.0
                del one_layer_lstm["monolithic"]

                self.lstm = nn.ModuleList(
                    [
                        nn.LSTM(
                            base_feature_dim if i == 0 else lstm["hidden_size"] * (2 if lstm["bidirectional"] else 1),
                            **one_layer_lstm,
                        )
                        for i in range(num_layers)
                    ]
                )

        if linear["num_layers"] < 1:
            return

        if lstm["num_layers"] > 0:
            linear_in_features = self.hparams.lstm["hidden_size"] * (2 if self.hparams.lstm["bidirectional"] else 1)
        else:
            linear_in_features = base_feature_dim

        self.linear = nn.ModuleList(
            [
                nn.Linear(in_features, out_features)
                for in_features, out_features in pairwise(
                    [
                        linear_in_features,
                    ]
                    + [self.hparams.linear["hidden_size"]] * self.hparams.linear["num_layers"]
                )
            ]
        )

    def build(self):
        if self.hparams.linear["num_layers"] > 0:
            in_features = self.hparams.linear["hidden_size"]
        elif self.hparams.lstm["num_layers"] > 0:
            in_features = self.hparams.lstm["hidden_size"] * (2 if self.hparams.lstm["bidirectional"] else 1)
        else:
            raise ValueError("where is PyanNet's head?")

        if self.specifications.powerset:
            out_features = self.specifications.num_powerset_classes
        else:
            out_features = len(self.specifications.classes)

        self.classifier = nn.Linear(in_features, out_features)
        self.activation = self.default_activation()

    def forward(self, waveforms: torch.Tensor) -> torch.Tensor:
        """Pass forward

        Parameters
        ----------
        waveforms : (batch, channel, sample)

        Returns
        -------
        scores : (batch, frame, classes)
        """

        outputs = self.sincnet(waveforms)
        outputs = rearrange(outputs, "batch feature frame -> batch frame feature")

        if self.hparams.lstm["num_layers"] > 0:
            if self.hparams.lstm["monolithic"]:
                outputs, _ = self.lstm(outputs)
            else:
                for i, lstm in enumerate(self.lstm):
                    outputs, _ = lstm(outputs)
                    if i + 1 < self.hparams.lstm["num_layers"]:
                        outputs = self.dropout(outputs)

        if self.hparams.linear["num_layers"] > 0:
            for linear in self.linear:
                outputs = F.leaky_relu(linear(outputs))

        return self.activation(self.classifier(outputs))


class PyanNet(PyanNetBase):
    """
    Standard PyanNet segmentation module based on SincNet.

    Parameters:
    sincnet : dict, optional
        Keyword arugments passed to the SincNet block.
        Defaults to {"stride": 10}.

    **kwargs : parameters for PyanNetBase
    """

    SINCNET_DEFAULTS = {"stride": 10}

    def __init__(
        self,
        sincnet: dict = None,
        **kwargs,
    ):
        sincnet = merge_dict(self.SINCNET_DEFAULTS, sincnet)
        sincnet["sample_rate"] = kwargs.get("sample_rate", 16000)
        self.save_hyperparameters("sincnet")

        super().__init__(base_net=SincNet(**self.hparams.sincnet), base_feature_dim=60, **kwargs)


class WavLMWrapper(nn.Module):
    def __init__(self, wavlm: WavLMModel, use_weighted_sum: bool = False):
        super().__init__()
        self.wavlm = wavlm
        self.use_weighted_sum = use_weighted_sum
        
        if self.use_weighted_sum:
            self.wsum = nn.Linear(self.wavlm.config.num_hidden_layers + 1, 1)

    def forward(self, wavs):
        # squeeze channel dimension if present
        if wavs.ndim > 2:
            wavs = wavs.squeeze(1)

        att_masks = torch.ones_like(wavs).to(torch.int32)
        outputs = self.wavlm(input_values=wavs, attention_mask=att_masks)

        if self.use_weighted_sum:
            stacked = torch.stack(outputs.hidden_states, dim=-1)
            summed = self.wsum(stacked).squeeze(-1)
            return rearrange(summed, "batch frame feature -> batch feature frame")
        else:
            return rearrange(outputs.last_hidden_state, "batch frame feature -> batch feature frame")


class PyanNetWavLM(PyanNetBase):
    """
    PyanNet implementation which uses pre-trained WavLM as base network.

    Parameters:
    wavlm : Union[WavLMModel, str]
        WavLM model or path to the pretrained model (local or Huggingface)
    freeze_wavlm_weights : bool
        Whether to freeze WavLM weights during training (default `True`)

    **kwargs : parameters for PyanNetBase
    """

    def __init__(
        self,
        wavlm: Union[WavLMModel, str],
        freeze_wavlm_weights: bool = True,
        use_weighted_sum: bool = False,
        **kwargs,
    ):
        if not TRANSFORMERS_IS_AVAILABLE:
            raise ImportError("Please install transformers to use PyanNetWavLM")

        if isinstance(wavlm, str):
            wavlm = WavLMModel.from_pretrained(wavlm)

        self.save_hyperparameters("freeze_wavlm_weights", "use_weighted_sum")
        super().__init__(
            base_net=WavLMWrapper(wavlm, use_weighted_sum=use_weighted_sum),
            base_feature_dim=wavlm.config.hidden_size,
            **kwargs,
        )

    def build(self):
        super().build()
        if self.hparams.freeze_wavlm_weights:
            self.freeze_by_name("sincnet.wavlm")
