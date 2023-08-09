import numpy as np
import torch

from PIL import Image
from diffusers import AutoencoderKL, UNet2DConditionModel
from diffusers.schedulers import KarrasDiffusionSchedulers
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer
from typing import List, Optional, Union

from .base_diffusion import BaseDiffusion


class ImageToImageDiffusion(BaseDiffusion):
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

    def embedding_to_latent(
        self,
        embedding: torch.FloatTensor,
        num_inference_steps: int,
        start_step: int,
        guidance_scale: float,
        guidance_rescale: float,
        latent: torch.FloatTensor,
        return_latent_history: bool = False,
    ) -> Union[torch.FloatTensor, List[torch.FloatTensor]]:
        """
        Generate latent by conditioning on prompt embedding and input image using diffusion.

        Parameters
        ----------
        embedding: torch.FloatTensor
            CLIP embedding of text prompt.
        num_inference_steps: int
            Number of diffusion steps to run.
        start_step: int
            Step to start diffusion from. The higher the value, the more similar the generated
            image will be to the input image.
        guidance_scale: float
            Guidance scale encourages the model to generate images following the prompt
            closely, albeit at the cost of image quality.
        guidance_rescale: float
            Guidance rescale from [Common Diffusion Noise Schedules and Sample Steps are
            Flawed](https://arxiv.org/pdf/2305.08891.pdf).
        latent: torch.FloatTensor
            Latent to start diffusion from.
        return_latent_history: bool
            Whether to return the latent history. If True, return list of all latents
            generated during diffusion steps.

        Returns
        -------
        Union[torch.FloatTensor, List[torch.FloatTensor]]
            Latent generated by diffusion. If return_latent_history is True, return list of
            all latents generated during diffusion steps.
        """

        latent = latent.to(self.device)
        self.scheduler.set_timesteps(num_inference_steps)

        if start_step > 0:
            start_timestep = (
                self.scheduler.timesteps[start_step].repeat(latent.shape[0]).long()
            )
            noise = torch.randn(latent.shape).to(self.device)
            latent = self.scheduler.add_noise(latent, noise, start_timestep)

        timesteps = self.scheduler.timesteps[start_step:]
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

    @torch.no_grad()
    def __call__(
        self,
        image: Image.Image,
        prompt: Union[str, List[str]],
        num_inference_steps: int = 50,
        start_step: int = 0,
        guidance_scale: float = 7.5,
        guidance_rescale: float = 0.7,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        output_type: str = "pil",
        return_latent_history: bool = False,
    ) -> Union[torch.Tensor, np.ndarray, List[Image.Image]]:
        """Generate image(s) from image(s)."""

        self.validate_input(
            prompt=prompt,
            negative_prompt=negative_prompt,
            start_step=start_step,
            num_inference_steps=num_inference_steps,
        )
        embedding = self.prompt_to_embedding(
            prompt=prompt,
            negative_prompt=negative_prompt,
        )
        image_latent = self.image_to_latent(image)
        latent = self.embedding_to_latent(
            embedding=embedding,
            image=image,
            num_inference_steps=num_inference_steps,
            start_step=start_step,
            guidance_scale=guidance_scale,
            guidance_rescale=guidance_rescale,
            latent=image_latent,
            return_latent_history=return_latent_history,
        )

        return self.resolve_output(
            latent=latent,
            output_type=output_type,
            return_latent_history=return_latent_history,
        )

    generate = __call__
