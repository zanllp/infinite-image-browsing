from PIL import Image
import json

from scripts.iib.parsers.model import ImageGenerationInfo, ImageGenerationParams
from scripts.iib.tool import omit, parse_generation_parameters, unique_by

class InvokeAIParser:
    def __init__(self):
        pass

    @classmethod
    def parse(clz, img: Image, file_path):
        if not clz.test(img, file_path):
            raise Exception("The input image does not match the current parser.")
        if 'invokeai_metadata' in img.info:
            core_metadata = json.loads(img.info['invokeai_metadata'])
            core_metadata.pop("canvas_v2_metadata", None)
        elif 'invokeai_graph' in img.info:
            raw_infos = json.loads(img.info["invokeai_graph"])
            core_metadata = {}
            for key in raw_infos['nodes']:
                if key.startswith("core_metadata"):
                    core_metadata = raw_infos['nodes'][key]
                    break
            
        positive_prompt = core_metadata.get("positive_prompt", "None")
        negative_prompt = core_metadata.get("negative_prompt", "None")
        steps = core_metadata.get("steps", 'Unknown')
        cfg_scale = core_metadata.get("cfg_scale", 'Unknown')
        model_name = core_metadata.get("model", {}).get("name", "Unknown")
        model_hash = core_metadata.get("model", {}).get("hash", "Unknown")
        
        meta_kv = [
            f"Steps: {steps}",
            "Source Identifier: InvokeAI",
            f"CFG scale: {cfg_scale}",
            f"Model: {model_name}",
            f"Model hash: {model_hash}",
        ]

        for key in core_metadata:
            if key not in ["positive_prompt", "negative_prompt", "steps", "cfg_scale", "model"]:
                val = core_metadata[key]
                if bool(val):
                    meta_kv.append(f"{key}: {val}")
        
        meta = ", ".join(meta_kv)
        meta_obj = {}
        for kv in meta_kv:
            k, v = kv.split(": ", 1)
            meta_obj[k] = v
        info = f"{positive_prompt}\nNegative prompt: {negative_prompt}\n{meta}"
        
        return ImageGenerationInfo(
            info,
            ImageGenerationParams(
                meta=meta_obj
                | {"final_width": img.size[0], "final_height": img.size[1]},
                pos_prompt=parse_generation_parameters(info)["pos_prompt"],
                # extra=params
            ),
        )

    @classmethod
    def test(clz, img: Image, file_path: str):
        try:
            return 'invokeai_graph' in img.info or 'invokeai_metadata' in img.info
        except Exception as e:
            return False
