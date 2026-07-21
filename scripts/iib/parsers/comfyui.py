import json
import os
from typing import Any, Dict

import piexif
import piexif.helper
from PIL import Image

from scripts.iib.tool import (
    _extract_usercomment_from_raw_exif,
    find,
    parse_generation_parameters,
    parse_prompt,
    read_sd_webui_gen_info_from_image,
    unique_by,
)
from scripts.iib.parsers.model import ImageGenerationInfo, ImageGenerationParams
from scripts.iib.logger import logger


def _find_comfyui_exif_tags(exif_bytes: bytes):
    """Find workflow: and prompt: strings in EXIF data using piexif.load()."""
    try:
        loaded = piexif.load(exif_bytes)
    except Exception:
        return None, None

    workflow_str = None
    prompt_str = None

    # Check IFD0 (0th) for workflow: and prompt: strings
    ifd0 = loaded.get('0th', {})
    for tag_id, val in ifd0.items():
        if isinstance(val, bytes):
            try:
                decoded = val.decode('utf-8', errors='ignore')
            except Exception:
                continue
            if decoded.lower().startswith('workflow:'):
                workflow_str = decoded
            elif decoded.lower().startswith('prompt:'):
                prompt_str = decoded

    return workflow_str, prompt_str


def is_img_created_by_comfyui(img: Image):
    if img.format == "PNG":
        prompt = img.info.get('prompt') or img.info.get('parameters')
        return prompt and (img.info.get('workflow') or ("class_type" in prompt)) # ermanitu
    elif img.format == "WEBP" or img.format == "JPEG":
        exif = img.info.get("exif")
        if not exif:
            return False

        # Primary: use piexif.load() to properly parse structured EXIF
        workflow_str, prompt_str = _find_comfyui_exif_tags(exif)
        if workflow_str and prompt_str:
            try:
                workflow = json.loads(workflow_str.split(":", 1)[1])
                prompt = json.loads(prompt_str.split(":", 1)[1])
                return (
                    workflow
                    and prompt
                    and any("class_type" in x.keys() for x in prompt.values())
                )
            except Exception:
                pass

        # Fallback: split by null bytes (for some non-standard EXIF encodings)
        split = [x.decode("utf-8", errors="ignore") for x in exif.split(b"\x00")]
        workflow_str = find(split, lambda x: x.lower().startswith("workflow:"))
        prompt_str = find(split, lambda x: x.lower().startswith("prompt:"))
        if workflow_str and prompt_str:
            try:
                workflow = json.loads(workflow_str.split(":", 1)[1])
                prompt = json.loads(prompt_str.split(":", 1)[1])
                return (
                    workflow
                    and prompt
                    and any("class_type" in x.keys() for x in prompt.values())
                )
            except Exception:
                pass

        # Fallback: non-standard EXIF (e.g. UserComment in IFD0 with type BYTE)
        # where null-byte split produces single chars instead of meaningful entries
        raw = _extract_usercomment_from_raw_exif(exif)
        if raw:
            try:
                decoded = raw.decode("utf-16", errors="ignore").strip('\x00')
            except Exception:
                decoded = raw.decode("utf-8", errors="ignore").strip('\x00')
            if decoded:
                wf = find([decoded], lambda x: x.lower().startswith("workflow:"))
                pt = find([decoded], lambda x: x.lower().startswith("prompt:"))
                if wf and pt:
                    try:
                        workflow = json.loads(wf.split(":", 1)[1])
                        prompt = json.loads(pt.split(":", 1)[1])
                        return workflow and prompt and any(
                            "class_type" in x.keys() for x in prompt.values()
                        )
                    except Exception:
                        pass
        return False
    else:
        return False  # unsupported format

def _has_webui_gen_info(img: Image) -> bool:
    """Check if image has SD WebUI generation info (parameters)."""
    if img.info.get('parameters'):
        return True
    # For WebP/JPEG, parameters may be in EXIF UserComment
    if img.format in ("WEBP", "JPEG"):
        exif = img.info.get("exif")
        if exif:
            try:
                loaded = piexif.load(exif)
                exif_section = loaded.get('Exif', {})
                user_comment = exif_section.get(piexif.ExifIFD.UserComment, b'')
                if user_comment:
                    try:
                        text = piexif.helper.UserComment.load(user_comment)
                        if text:
                            return True
                    except Exception:
                        pass
                    # Try raw decode fallback
                    raw = _extract_usercomment_from_raw_exif(exif)
                    if raw:
                        try:
                            decoded = raw.decode("utf-16", errors="ignore").strip('\x00')
                        except Exception:
                            decoded = raw.decode("utf-8", errors="ignore").strip('\x00')
                        if decoded:
                            return True
            except Exception:
                pass
    return False


def is_img_created_by_comfyui_with_webui_gen_info(img: Image):
    return is_img_created_by_comfyui(img) and _has_webui_gen_info(img)



def extract_comfyui_prompt_with_wildcard_support(data: Dict, KSampler_entry: Dict):
    """
    Enhanced prompt extraction for workflows using ImpactWildcardProcessor.

    This function handles:
    - populated_text over wildcard_text
    - Recursive resolution of upstream prompt nodes
    - Intermediate conditioning nodes such as FluxGuidance

    Returns:
        tuple of (positive_prompt, negative_prompt)
    """

    def get_node(node_id):
        return data.get(str(node_id)) or data.get(node_id)

    def normalize_text(value):
        if not isinstance(value, str):
            return None
        return value.strip()

    def extract_direct_text(node):
        if not isinstance(node, dict):
            return None, False

        class_type = node.get("class_type", "")
        inputs = node.get("inputs", {}) or {}

        if class_type == "ImpactWildcardProcessor":
            populated = normalize_text(inputs.get("populated_text"))
            return (populated or ""), True

        if "CLIPTextEncode" in class_type:
            for key in ("text", "t5xxl"):
                if key in inputs:
                    value = inputs.get(key)
                    if isinstance(value, str):
                        return value.strip(), True
                    if isinstance(value, list) and len(value) >= 1:
                        return None, False
                    return "", True

        for key in ("text", "t5xxl", "prompt", "string", "value"):
            if key in inputs and isinstance(inputs.get(key), str):
                return inputs.get(key).strip(), True

        return None, False

    def resolve_text_from_ref(ref, visited=None):
        if visited is None:
            visited = set()

        node_id = ref[0] if isinstance(ref, list) and len(ref) >= 1 else ref
        node_key = str(node_id)
        if not node_key or node_key in visited:
            return ""
        visited.add(node_key)

        node = get_node(node_id)
        if not isinstance(node, dict):
            return ""

        direct_text, is_terminal = extract_direct_text(node)
        if direct_text is not None:
            return direct_text
        if is_terminal:
            return ""

        inputs = node.get("inputs", {}) or {}
        class_type = node.get("class_type", "")

        if class_type == "FluxGuidance":
            conditioning = inputs.get("conditioning")
            if isinstance(conditioning, list) and len(conditioning) >= 1:
                return resolve_text_from_ref(conditioning, visited)
            return ""

        for key in ("text", "t5xxl", "conditioning", "positive", "negative", "prompt", "string", "value"):
            value = inputs.get(key)
            if isinstance(value, list) and len(value) >= 1:
                resolved = resolve_text_from_ref(value, visited)
                if resolved or resolved == "":
                    return resolved

        return ""

    try:
        positive_ref = KSampler_entry.get("positive")
        negative_ref = KSampler_entry.get("negative")

        positive_prompt = resolve_text_from_ref(positive_ref) if positive_ref else ""
        negative_prompt = resolve_text_from_ref(negative_ref) if negative_ref else ""

        return positive_prompt or "", negative_prompt or ""
    except Exception as e:
        print(e)
        return "", ""


def get_comfyui_exif_data(img: Image):
    prompt = None
    if img.format == "PNG":
        prompt = img.info.get('prompt')
    elif img.format == "WEBP" or img.format == "JPEG":
        exif = img.info.get("exif")
        if exif:
            # Primary: use piexif.load() to properly parse structured EXIF
            _, prompt_str = _find_comfyui_exif_tags(exif)
            if prompt_str:
                prompt = prompt_str.split(":", 1)[1]
            else:
                # Fallback: split by null bytes (for some non-standard EXIF encodings)
                split = [x.decode("utf-8", errors="ignore") for x in exif.split(b"\x00")]
                prompt_str = find(split, lambda x: x.lower().startswith("prompt:"))
                if prompt_str:
                    prompt = prompt_str.split(":", 1)[1] if prompt_str else None
    if not prompt:
        return {}

    data: Dict[str, Any] = json.loads(prompt)
    meta_key = '3'
    for i in data.keys():
        try:
            if data[i]["class_type"].startswith("KSampler"):
                meta_key = i
                break
        except Exception:
            pass

    if meta_key not in data:
        return {}

    meta = {}
    KSampler_entry = data[meta_key]["inputs"]

    # As a workaround to bypass parsing errors in the parser.
    # https://github.com/jiw0220/stable-diffusion-image-metadata/blob/00b8d42d4d1a536862bba0b07c332bdebb2a0ce5/src/index.ts#L130
    meta["Steps"] = KSampler_entry.get("steps", "Unknown")
    meta["Sampler"] = KSampler_entry.get("sampler_name", "Unknown")
    try:
        meta["Model"] = data[KSampler_entry["model"][0]]["inputs"].get("ckpt_name")
    except Exception:
        meta["Model"] = None
    meta["Source Identifier"] = "ComfyUI"

    def get_text_from_clip(idx: str):
        try:
            inputs = data[idx]["inputs"]
            if "text" in inputs:
                text = inputs["text"]
            elif "t5xxl" in inputs:
                text = inputs["t5xxl"]
            else:
                return ""
            if isinstance(text, list):  # type:CLIPTextEncode (NSP) mode:Wildcards
                text = data[text[0]]["inputs"]["text"]
            return text.strip()
        except Exception as e:
            print(e)
            return ""

    has_impact_wildcard = any(
        node_data.get("class_type") == "ImpactWildcardProcessor"
        for node_data in data.values()
        if isinstance(node_data, dict)
    )

    # Detection Point 1: Check if workflow contains ImpactWildcardProcessor
    # If yes, immediately use the enhanced extraction and return
    if has_impact_wildcard:
        pos_prompt, neg_prompt = extract_comfyui_prompt_with_wildcard_support(
            data, KSampler_entry
        )
        pos_prompt_arr = unique_by(parse_prompt(pos_prompt)["pos_prompt"])
        return {
            "meta": meta,
            "pos_prompt": pos_prompt_arr,
            "pos_prompt_raw": pos_prompt,
            "neg_prompt_raw": neg_prompt
        }

    extract_all_prompts = os.getenv("IIB_COMFYUI_EXTRACT_ALL_PROMPTS", "false").lower() == "true"

    if extract_all_prompts:
        # 注意：此模式下无法自动区分正向和负向提示词，会全部归到正向提示词中
        # 如需区分正负向，需根据工作流结构调整解析逻辑
        all_prompts = []
        for node_id, node_data in data.items():
            try:
                class_type = node_data.get("class_type", "")
                inputs = node_data.get("inputs", {})

                if "CLIPTextEncode" in class_type:
                    text = inputs.get("text", "")
                    if isinstance(text, list):
                        text = data[text[0]]["inputs"].get("text", "")
                    if text:
                        all_prompts.append(text.strip())
            except Exception as e:
                print(e)
                pass

        all_prompts_str = "\nBREAK\n".join(all_prompts) if all_prompts else ""
        pos_prompt = all_prompts_str
        neg_prompt = ""
    else:
        in_node = data[str(KSampler_entry["positive"][0])]
        if in_node["class_type"] != "FluxGuidance":
            pos_prompt = get_text_from_clip(KSampler_entry["positive"][0])
        else:
            pos_prompt = get_text_from_clip(in_node["inputs"]["conditioning"][0])

        neg_prompt = get_text_from_clip(KSampler_entry["negative"][0])

    pos_prompt_arr = unique_by(parse_prompt(pos_prompt)["pos_prompt"])

    # Detection Point 2: Fallback if no prompts were extracted
    # If standard extraction failed, try the enhanced method
    if has_impact_wildcard and (not pos_prompt_arr or not pos_prompt.strip()):
        pos_prompt_fallback, neg_prompt_fallback = extract_comfyui_prompt_with_wildcard_support(
            data, KSampler_entry
        )
        if pos_prompt_fallback:
            pos_prompt = pos_prompt_fallback
            pos_prompt_arr = unique_by(parse_prompt(pos_prompt_fallback)["pos_prompt"])

        if neg_prompt_fallback:
            neg_prompt = neg_prompt_fallback

    return {
        "meta": meta,
        "pos_prompt": pos_prompt_arr,
        "pos_prompt_raw": pos_prompt,
        "neg_prompt_raw": neg_prompt
    }

def comfyui_exif_data_to_str(data):
    res = data["pos_prompt_raw"] + "\nNegative prompt: " + data["neg_prompt_raw"] + "\n"
    meta_arr = []
    for k,v in data["meta"].items():
        meta_arr.append(f'{k}: {v}')
    return res + ", ".join(meta_arr)


class ComfyUIParser:
    def __init__(self):
        pass

    @classmethod
    def parse(clz, img, file_path):
        info = ""
        params = None
        if not clz.test(img, file_path):
            raise Exception("The input image does not match the current parser.")
        width, height = img.size
        try:
            if is_img_created_by_comfyui_with_webui_gen_info(img):
                info = read_sd_webui_gen_info_from_image(img, file_path)
                info += ", Source Identifier: ComfyUI"
                params = parse_generation_parameters(info)
            else:
                params = get_comfyui_exif_data(img)
                info = comfyui_exif_data_to_str(params)
        except Exception as e:
            logger.error("parse comfyui image failed. prompt:", exc_info=e)
            logger.error(img.info.get("prompt"))
            return ImageGenerationInfo(
                params=ImageGenerationParams(
                    meta={"final_width": width, "final_height": height}
                )
            )
        return ImageGenerationInfo(
            info,
            ImageGenerationParams(
                meta=params["meta"] | {"final_width": width, "final_height": height},
                pos_prompt=params["pos_prompt"],
                extra=params,
            ),
        )

    @classmethod
    def test(clz, img: Image, file_path: str) -> bool:
        try:
            return is_img_created_by_comfyui(
                img
            ) or is_img_created_by_comfyui_with_webui_gen_info(img)
        except Exception:
            return False
