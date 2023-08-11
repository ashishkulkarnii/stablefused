import numpy as np
import torch

from PIL import Image
from diffusers import AutoencoderKL, UNet2DConditionModel
from diffusers.schedulers import KarrasDiffusionSchedulers
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer
from typing import List, Optional, Union

from stablefused.diffusion import BaseDiffusion
from stablefused.utils import lerp, slerp


class LatentWalkDiffusion(BaseDiffusion):
    def __init__(
        self,
        model_id: str = None,
        tokenizer: CLIPTokenizer = None,
        text_encoder: CLIPTextModel = None,
        vae: AutoencoderKL = None,
        unet: UNet2DConditionModel = None,
        scheduler: KarrasDiffusionSchedulers = None,
        name: str = None,
        torch_dtype: torch.dtype = torch.float32,
        device="cuda",
    ) -> None:
        super().__init__(
            model_id=model_id,
            tokenizer=tokenizer,
            text_encoder=text_encoder,
            vae=vae,
            unet=unet,
            scheduler=scheduler,
            name=name,
            torch_dtype=torch_dtype,
            device=device,
        )

    def modify_latent(
        self,
        latent: torch.FloatTensor,
        strength: float,
    ) -> torch.FloatTensor:
        """Modify latent with strength."""

        noise = torch.randn(latent.shape).to(self.device)
        new_latent = (1 - strength) * latent + strength * noise
        new_latent = (new_latent - new_latent.mean()) / new_latent.std()

        return new_latent

    def embedding_to_latent(
        self,
        embedding: torch.FloatTensor,
        num_inference_steps: int,
        guidance_scale: float,
        guidance_rescale: float,
        latent: torch.FloatTensor,
        return_latent_history: bool = False,
    ) -> Union[torch.FloatTensor, List[torch.FloatTensor]]:
        """
        Generate latent by conditioning on prompt embedding using diffusion.

        Parameters
        ----------
        embedding: torch.FloatTensor
            CLIP embedding of text prompt.
        num_inference_steps: int
            Number of diffusion steps to run.
        guidance_scale: float
            Guidance scale encourages the model to generate images following the prompt
            closely, albeit at the cost of image quality.
        guidance_rescale: float
            Guidance rescale from [Common Diffusion Noise Schedules and Sample Steps are
            Flawed](https://arxiv.org/pdf/2305.08891.pdf).
        latent: torch.FloatTensor
            Latent to start diffusion from.
        return_latent_history: bool
            Whether to return latent history. If True, return list of all latents
            generated during diffusion steps.

        Returns
        -------
        Union[torch.FloatTensor, List[torch.FloatTensor]]
            Latent generated by diffusion. If return_latent_history is True, return list of
            all latents generated during diffusion steps.
        """

        latent = latent.to(self.device)
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps = self.scheduler.timesteps

        latent = latent * self.scheduler.init_noise_sigma
        latent_history = [latent]

        for i, timestep in tqdm(list(enumerate(timesteps))):
            latent_model_input = torch.cat([latent] * 2)
            latent_model_input = self.scheduler.scale_model_input(
                latent_model_input, timestep
            )

            noise_prediction = self.unet(
                latent_model_input,
                timestep,
                encoder_hidden_states=embedding,
                return_dict=False,
            )[0]

            noise_prediction = self.do_classifier_free_guidance(
                noise_prediction, guidance_scale, guidance_rescale
            )

            latent = self.scheduler.step(
                noise_prediction, timestep, latent, return_dict=False
            )[0]

            if return_latent_history:
                latent_history.append(latent)

        return latent_history if return_latent_history else latent

    def interpolate_embedding(
        self,
        embedding: torch.FloatTensor,
        interpolation_steps: Union[int, List[int]],
        interpolation_type: str,
    ) -> torch.FloatTensor:
        """
        Interpolate embedding.

        Parameters
        ----------
        embedding: torch.FloatTensor
            CLIP embedding of text prompt.
        interpolation_steps: Union[int, List[int]]
            Number of interpolation steps to run.
        embedding_interpolation_type: str
            Type of interpolation to run. One of ["lerp", "slerp"].

        Returns
        -------
        torch.FloatTensor
            Interpolated embedding.
        """

        if interpolation_type == "lerp":
            interpolation_fn = lerp
        elif interpolation_type == "slerp":
            interpolation_fn = slerp
        else:
            raise ValueError(
                f"embedding_interpolation_type must be one of ['lerp', 'slerp'], got {interpolation_type}."
            )

        unconditional_embedding, text_embedding = embedding.chunk(2)
        steps = torch.linspace(
            0, 1, interpolation_steps, dtype=embedding.dtype, device=embedding.device
        ).numpy()
        steps = np.expand_dims(steps, axis=tuple(range(1, text_embedding.ndim)))
        interpolations = []

        for i in range(text_embedding.shape[0] - 1):
            interpolations.append(
                interpolation_fn(
                    text_embedding[i], text_embedding[i + 1], steps
                ).squeeze(dim=1)
            )
        interpolations = torch.cat(interpolations)

        # TODO: Think of a better way of doing this
        # It can be done because all unconditional embeddings are the same
        single_unconditional_embedding = unconditional_embedding[0].unsqueeze(dim=0)
        unconditional_embedding = single_unconditional_embedding.repeat(
            interpolations.shape[0], 1, 1
        )
        interpolations = torch.cat([unconditional_embedding, interpolations])

        return interpolations

    def interpolate_latent(
        self,
        latent: torch.FloatTensor,
        interpolation_steps: Union[int, List[int]],
        interpolation_type: str,
    ) -> torch.FloatTensor:
        """
        Interpolate latent.

        Parameters
        ----------
        latent: torch.FloatTensor
            Latent to interpolate.
        interpolation_steps: Union[int, List[int]]
            Number of interpolation steps to run.
        latent_interpolation_type: str
            Type of interpolation to run. One of ["lerp", "slerp"].

        Returns
        -------
        torch.FloatTensor
            Interpolated latent.
        """

        if interpolation_type == "lerp":
            interpolation_fn = lerp
        elif interpolation_type == "slerp":
            interpolation_fn = slerp

        steps = torch.linspace(0, 1, interpolation_steps, dtype=latent.dtype).numpy()
        steps = np.expand_dims(steps, axis=tuple(range(1, latent.ndim)))
        interpolations = []

        for i in range(latent.shape[0] - 1):
            interpolations.append(
                interpolation_fn(latent[i], latent[i + 1], steps).squeeze(dim=1)
            )

        return torch.cat(interpolations)

    @torch.no_grad()
    def __call__(
        self,
        prompt: Union[str, List[str]],
        latent: torch.FloatTensor,
        strength: float = 0.2,
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        guidance_rescale: float = 0.7,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        output_type: str = "pil",
        return_latent_history: bool = False,
    ) -> Union[torch.Tensor, np.ndarray, List[Image.Image]]:
        """Walk latent space from latent to generate similar image(s)."""

        self.validate_input(
            prompt=prompt,
            negative_prompt=negative_prompt,
            strength=strength,
        )
        embedding = self.prompt_to_embedding(
            prompt=prompt,
            negative_prompt=negative_prompt,
        )
        latent = self.modify_latent(latent, strength)
        latent = self.embedding_to_latent(
            embedding=embedding,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            guidance_rescale=guidance_rescale,
            latent=latent,
            return_latent_history=return_latent_history,
        )

        return self.resolve_output(
            latent=latent,
            output_type=output_type,
            return_latent_history=return_latent_history,
        )

    generate = __call__

    @torch.no_grad()
    def interpolate(
        self,
        prompt: List[str],
        latent: torch.FloatTensor,
        num_inference_steps: int = 50,
        interpolation_steps: Union[int, List[int]] = 8,
        guidance_scale: float = 7.5,
        guidance_rescale: float = 0.7,
        negative_prompt: Optional[List[str]] = None,
        output_type: str = "pil",
        return_latent_history: bool = False,
        embedding_interpolation_type: str = "lerp",
        latent_interpolation_type: str = "slerp",
    ) -> Union[torch.Tensor, np.ndarray, List[Image.Image]]:
        self.validate_input(
            prompt=prompt,
            negative_prompt=negative_prompt,
        )

        if not isinstance(prompt, list):
            raise ValueError(f"prompt must be a list of strings, not {type(prompt)}")
        if len(prompt) < 2:
            raise ValueError(
                f"prompt must be a list of at least 2 strings, not {len(prompt)}"
            )
        if isinstance(interpolation_steps, int):
            pass
            # interpolation_steps = [interpolation_steps] * (len(prompt) - 1)
        elif isinstance(interpolation_steps, list):
            if len(interpolation_steps) != len(prompt) - 1:
                raise ValueError(
                    f"interpolation_steps must be a list of length len(prompt) - 1, not {len(interpolation_steps)}"
                )
            raise NotImplementedError(
                "interpolation_steps as a list is not yet implemented"
            )
        else:
            raise ValueError(
                f"interpolation_steps must be an int or list, not {type(interpolation_steps)}"
            )

        embedding = self.prompt_to_embedding(
            prompt=prompt,
            negative_prompt=negative_prompt,
        )
        interpolated_embedding = self.interpolate_embedding(
            embedding=embedding,
            interpolation_steps=interpolation_steps,
            interpolation_type=embedding_interpolation_type,
        )
        interpolated_latent = self.interpolate_latent(
            latent=latent,
            interpolation_steps=interpolation_steps,
            interpolation_type=latent_interpolation_type,
        )
        latent = self.embedding_to_latent(
            embedding=interpolated_embedding,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            guidance_rescale=guidance_rescale,
            latent=interpolated_latent,
            return_latent_history=return_latent_history,
        )

        return self.resolve_output(
            latent=latent,
            output_type=output_type,
            return_latent_history=return_latent_history,
        )
