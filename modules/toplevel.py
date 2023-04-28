import torch
import torch.nn.functional as F

from basics.base_module import CategorizedModule
from modules.commons.common_layers import (
    XavierUniformInitLinear as Linear,
)
from modules.diffusion.ddpm import GaussianDiffusion, RepetitiveDiffusion, CurveDiffusion1d, CurveDiffusion2d
from modules.fastspeech.acoustic_encoder import FastSpeech2Acoustic
from modules.fastspeech.tts_modules import LengthRegulator, VariancePredictor
from modules.fastspeech.variance_encoder import FastSpeech2Variance
from utils.hparams import hparams


class DiffSingerAcoustic(CategorizedModule):
    def __init__(self, vocab_size, out_dims):
        super().__init__()
        self.fs2 = FastSpeech2Acoustic(
            vocab_size=vocab_size
        )
        self.diffusion = GaussianDiffusion(
            out_dims=out_dims,
            timesteps=hparams['timesteps'],
            k_step=hparams['K_step'],
            denoiser_type=hparams['diff_decoder_type'],
            denoiser_args=(
                hparams['residual_layers'],
                hparams['residual_channels']
            ),
            spec_min=hparams['spec_min'],
            spec_max=hparams['spec_max']
        )

    @property
    def category(self):
        return 'acoustic'

    def forward(self, txt_tokens, mel2ph, f0, key_shift=None, speed=None,
                spk_embed_id=None, gt_mel=None, infer=True, **kwargs):
        condition = self.fs2(txt_tokens, mel2ph, f0, key_shift=key_shift, speed=speed,
                             spk_embed_id=spk_embed_id, **kwargs)
        if infer:
            mel = self.diffusion(condition, infer=True)
            mel *= ((mel2ph > 0).float()[:, :, None])
            return mel
        else:
            loss = self.diffusion(condition, gt_spec=gt_mel, infer=False)
            return loss


class DiffSingerVariance(CategorizedModule):
    def __init__(self, vocab_size):
        super().__init__()
        self.fs2 = FastSpeech2Variance(
            vocab_size=vocab_size
        )
        self.lr = LengthRegulator()

        if hparams['predict_pitch']:
            pitch_hparams = hparams['pitch_prediction_args']
            self.base_pitch_embed = Linear(1, hparams['hidden_size'])
            diff_predictor_mode = pitch_hparams['diff_predictor_mode']
            if diff_predictor_mode == 'repeat':
                self.pitch_predictor = RepetitiveDiffusion(
                    vmin=pitch_hparams['pitch_delta_vmin'],
                    vmax=pitch_hparams['pitch_delta_vmax'],
                    repeat_bins=pitch_hparams['num_pitch_bins'],
                    timesteps=hparams['timesteps'],
                    k_step=hparams['K_step'],
                    denoiser_type=hparams['diff_decoder_type'],
                    denoiser_args=(
                        pitch_hparams['residual_layers'],
                        pitch_hparams['residual_channels']
                    )
                )
            elif diff_predictor_mode == '1d':
                self.pitch_predictor = CurveDiffusion1d(
                    vmin=pitch_hparams['pitch_delta_vmin'],
                    vmax=pitch_hparams['pitch_delta_vmax'],
                    timesteps=hparams['timesteps'],
                    k_step=hparams['K_step'],
                    denoiser_type=hparams['diff_decoder_type'],
                    denoiser_args=(
                        pitch_hparams['residual_layers'],
                        pitch_hparams['residual_channels']
                    )
                )
            elif diff_predictor_mode == '2d':
                self.pitch_predictor = CurveDiffusion2d(
                    vmin=pitch_hparams['pitch_delta_vmin'],
                    vmax=pitch_hparams['pitch_delta_vmax'],
                    num_bins=pitch_hparams['num_pitch_bins'],
                    deviation=pitch_hparams['deviation'],
                    timesteps=hparams['timesteps'],
                    k_step=hparams['K_step'],
                    denoiser_type=hparams['diff_decoder_type'],
                    denoiser_args=(
                        pitch_hparams['residual_layers'],
                        pitch_hparams['residual_channels']
                    )
                )
            else:
                raise NotImplementedError()
            # from modules.fastspeech.tts_modules import PitchPredictor
            # self.pitch_predictor = PitchPredictor(
            #     vmin=pitch_hparams['pitch_delta_vmin'],
            #     vmax=pitch_hparams['pitch_delta_vmax'],
            #     num_bins=pitch_hparams['num_pitch_bins'],
            #     deviation=pitch_hparams['deviation'],
            #     in_dims=hparams['hidden_size'],
            #     n_chans=pitch_hparams['hidden_size']
            # )

        if hparams['predict_energy']:
            self.pitch_embed = Linear(1, hparams['hidden_size'])
            energy_hparams = hparams['energy_prediction_args']
            self.energy_predictor = RepetitiveDiffusion(
                vmin=10. ** (energy_hparams['db_vmin'] / 20.),
                vmax=10. ** (energy_hparams['db_vmax'] / 20.),
                repeat_bins=energy_hparams['num_repeat_bins'],
                timesteps=hparams['timesteps'],
                k_step=hparams['K_step'],
                denoiser_type=hparams['diff_decoder_type'],
                denoiser_args=(
                    energy_hparams['residual_layers'],
                    energy_hparams['residual_channels']
                )
            )
            # self.energy_predictor = VariancePredictor(
            #     in_dims=hparams['hidden_size'],
            #     n_chans=energy_hparams['hidden_size'],
            #     n_layers=energy_hparams['num_layers'],
            #     dropout_rate=energy_hparams['dropout'],
            #     padding=hparams['ffn_padding'],
            #     kernel_size=energy_hparams['kernel_size']
            # )

    @property
    def category(self):
        return 'variance'

    def forward(self, txt_tokens, midi, ph2word, ph_dur=None, word_dur=None,
                mel2ph=None, base_pitch=None, delta_pitch=None, energy=None, infer=True):
        encoder_out, dur_pred_out = self.fs2(
            txt_tokens, midi=midi, ph2word=ph2word,
            ph_dur=ph_dur, word_dur=word_dur, infer=infer
        )

        if not hparams['predict_pitch'] and not hparams['predict_energy']:
            return dur_pred_out, None, None

        if mel2ph is None or hparams['dur_cascade']:
            # (extract mel2ph from dur_pred_out)
            raise NotImplementedError()

        encoder_out = F.pad(encoder_out, [0, 0, 1, 0])
        mel2ph_ = mel2ph[..., None].repeat([1, 1, hparams['hidden_size']])
        condition = torch.gather(encoder_out, 1, mel2ph_)

        if hparams['predict_pitch']:
            pitch_cond = condition + self.pitch_embed(base_pitch[:, :, None])
            pitch_pred_out = self.pitch_predictor(pitch_cond, delta_pitch, infer)
        else:
            pitch_pred_out = None

        if hparams['predict_energy']:
            pitch_embed = self.pitch_embed((base_pitch + delta_pitch)[:, :, None])
            energy_cond = condition + pitch_embed
            energy_pred_out = self.energy_predictor(energy_cond, energy, infer)
        else:
            energy_pred_out = None

        return dur_pred_out, pitch_pred_out, energy_pred_out
