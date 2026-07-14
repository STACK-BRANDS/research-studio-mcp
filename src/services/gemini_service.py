import os
import sys
import time
import logging
from google import genai
from google.genai import types
from typing import Optional, List, Dict, Any

# Set up logger
logger = logging.getLogger(__name__)

GEMINI_API_KEY = None

# Model used for video analysis (Gemini 2.0 Flash is more cost-effective than Pro)
# The experimental "-exp" models get retired and 404. Use a current stable multimodal model;
# override via GEMINI_MODEL env if Google rotates names again.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Type alias for uploaded files (new google-genai SDK)
File = types.File

# Google rotates/retires Gemini model names constantly (gemini-2.0-flash-exp got 404'd;
# gemini-2.5-flash came back "no longer available to new users"). Instead of hardcoding one
# name, try a fallback chain and cache the first that works on THIS key.
_MODEL_CANDIDATES = [
    "gemini-2.5-flash-002",
    "gemini-2.5-flash",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash",
    "gemini-flash-latest",
    "gemini-1.5-flash-002",
    "gemini-1.5-flash",
]
# Error fragments that mean "this model name won't work — try the next one".
_MODEL_MISS = ("not found", "not available", "no longer available", "does not exist",
               "unsupported", "invalid model", "404", "permission")
_RESOLVED_MODEL = None


def _model_order():
    """Candidate models: the GEMINI_MODEL env override first (if set), then the chain."""
    order = []
    if _RESOLVED_MODEL:
        order.append(_RESOLVED_MODEL)
    env = os.getenv("GEMINI_MODEL")
    if env:
        order.append(env)
    order.extend(_MODEL_CANDIDATES)
    seen, out = set(), []
    for m in order:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def generate_with_fallback(client, contents):
    """generate_content that walks the candidate models until one is available on this key.
    Caches the winner for the rest of the process. Re-raises non-model errors immediately."""
    global _RESOLVED_MODEL
    last_err, tried = None, []
    for model in _model_order():
        try:
            resp = client.models.generate_content(model=model, contents=contents)
            if _RESOLVED_MODEL != model:
                logger.info(f"Gemini video model resolved to '{model}'")
                _RESOLVED_MODEL = model
            return resp
        except Exception as e:
            tried.append(model)
            last_err = e
            if any(frag in str(e).lower() for frag in _MODEL_MISS):
                logger.warning(f"Gemini model '{model}' unavailable, trying next")
                continue
            raise
    raise Exception(f"No available Gemini model on this key. Tried: {tried}. Last error: {last_err}")

def get_gemini_api_key() -> str:
    """
    Get Gemini API key from command line arguments or environment variable.
    Caches the key in memory after first read.
    Priority: command line argument > environment variable

    Returns:
        str: The Gemini API key.

    Raises:
        Exception: If no key is provided in command line arguments or environment.
    """
    global GEMINI_API_KEY
    if GEMINI_API_KEY is None:
        # Try command line argument first
        if "--gemini-api-key" in sys.argv:
            token_index = sys.argv.index("--gemini-api-key") + 1
            if token_index < len(sys.argv):
                GEMINI_API_KEY = sys.argv[token_index]
                logger.info(f"Using Gemini API key from command line arguments")
            else:
                raise Exception("--gemini-api-key argument provided but no key value followed it")
        # Try environment variable
        elif os.getenv("GEMINI_API_KEY"):
            GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
            logger.info(f"Using Gemini API key from environment variable")
        else:
            raise Exception("Gemini API key must be provided via '--gemini-api-key' command line argument or 'GEMINI_API_KEY' environment variable")

    return GEMINI_API_KEY


def configure_gemini() -> genai.Client:
    """
    Configure the Gemini client with the API key.

    Uses the modern google-genai SDK, which supports both the legacy ``AIza``
    keys and the newer ``AQ.`` prefixed keys now issued by Google AI Studio and
    Cloud Console.

    Returns:
        genai.Client: Configured Gemini client for video analysis
    """
    api_key = get_gemini_api_key()
    client = genai.Client(api_key=api_key)

    logger.info("Gemini API configured successfully")
    return client


def upload_video_to_gemini(client: genai.Client, video_path: str) -> File:
    """
    Upload a video file to Gemini File API for analysis.

    Args:
        client: Configured Gemini client
        video_path: Path to the video file to upload

    Returns:
        File: The uploaded file object for use in analysis

    Raises:
        Exception: If upload fails
    """
    try:
        # Upload video file
        video_file = client.files.upload(file=video_path)

        # Wait for processing to complete
        while video_file.state.name == "PROCESSING":
            time.sleep(2)
            video_file = client.files.get(name=video_file.name)

        if video_file.state.name == "FAILED":
            raise Exception(f"Video processing failed: {video_file.state}")

        logger.info(f"Video uploaded successfully: {video_file.name}")
        return video_file

    except Exception as e:
        logger.error(f"Failed to upload video to Gemini: {str(e)}")
        raise


def analyze_video_with_gemini(client: genai.Client, video_file: File, prompt: str) -> str:
    """
    Analyze a video using Gemini with a custom prompt.

    Args:
        client: Configured Gemini client
        video_file: Uploaded video file from Gemini File API
        prompt: Analysis prompt for the video

    Returns:
        str: Analysis results from Gemini

    Raises:
        Exception: If analysis fails
    """
    try:
        # Generate analysis
        response = generate_with_fallback(client, [video_file, prompt])

        if not response.text:
            raise Exception("Gemini returned empty response")

        logger.info("Video analysis completed successfully")
        return response.text

    except Exception as e:
        logger.error(f"Video analysis failed: {str(e)}")
        raise


def analyze_videos_batch_with_gemini(client: genai.Client, video_files: List[File], prompt_template: str, video_contexts: List[Dict[str, Any]]) -> List[str]:
    """
    Analyze multiple videos using Gemini in a single request for token efficiency.

    Args:
        client: Configured Gemini client
        video_files: List of uploaded video files from Gemini File API
        prompt_template: Base analysis prompt template
        video_contexts: List of context dicts with brand_name, ad_id, etc. for each video

    Returns:
        List[str]: Analysis results for each video in order

    Raises:
        Exception: If batch analysis fails
    """
    try:
        if not video_files or len(video_files) != len(video_contexts):
            raise Exception("Video files and contexts must have matching lengths")

        # Create batch prompt with multiple videos
        batch_prompt = f"""Analyze the following {len(video_files)} Facebook ad videos. For each video, provide analysis following this format:

{prompt_template}

Please analyze each video separately and clearly label each analysis as "VIDEO 1:", "VIDEO 2:", etc.

"""

        # Add context information for each video
        for i, context in enumerate(video_contexts, 1):
            brand_info = f" (Brand: {context.get('brand_name', 'Unknown')})" if context.get('brand_name') else ""
            ad_info = f" (Ad ID: {context.get('ad_id', 'Unknown')})" if context.get('ad_id') else ""
            batch_prompt += f"VIDEO {i}{brand_info}{ad_info}:\n"

        # Combine all video files with the prompt
        content_parts = [batch_prompt] + video_files

        # Generate batch analysis
        response = generate_with_fallback(client, content_parts)

        if not response.text:
            raise Exception("Gemini returned empty response for batch analysis")

        # Split response by video markers
        analysis_text = response.text
        video_analyses = []

        # Parse individual video analyses
        for i in range(1, len(video_files) + 1):
            video_marker = f"VIDEO {i}:"
            next_marker = f"VIDEO {i + 1}:" if i < len(video_files) else None

            start_idx = analysis_text.find(video_marker)
            if start_idx == -1:
                logger.warning(f"Could not find analysis for VIDEO {i}")
                video_analyses.append(f"Analysis not found in batch response for video {i}")
                continue

            start_idx += len(video_marker)

            if next_marker:
                end_idx = analysis_text.find(next_marker)
                individual_analysis = analysis_text[start_idx:end_idx].strip() if end_idx != -1 else analysis_text[start_idx:].strip()
            else:
                individual_analysis = analysis_text[start_idx:].strip()

            video_analyses.append(individual_analysis)

        logger.info(f"Batch video analysis completed successfully for {len(video_files)} videos")
        return video_analyses

    except Exception as e:
        logger.error(f"Batch video analysis failed: {str(e)}")
        raise


def upload_videos_batch_to_gemini(client: genai.Client, video_paths: List[str]) -> List[File]:
    """
    Upload multiple video files to Gemini File API for batch analysis.

    Args:
        client: Configured Gemini client
        video_paths: List of paths to video files to upload

    Returns:
        List[File]: List of uploaded file objects for use in analysis

    Raises:
        Exception: If any upload fails
    """
    uploaded_files = []
    failed_uploads = []

    try:
        for i, video_path in enumerate(video_paths):
            try:
                # Upload video file
                video_file = client.files.upload(file=video_path)

                # Wait for processing to complete
                while video_file.state.name == "PROCESSING":
                    time.sleep(2)
                    video_file = client.files.get(name=video_file.name)

                if video_file.state.name == "FAILED":
                    failed_uploads.append(f"Video {i+1}: {video_file.state}")
                    continue

                uploaded_files.append(video_file)
                logger.info(f"Video {i+1} uploaded successfully: {video_file.name}")

            except Exception as e:
                failed_uploads.append(f"Video {i+1}: {str(e)}")
                logger.error(f"Failed to upload video {i+1} at {video_path}: {str(e)}")

        if failed_uploads:
            error_msg = f"Some video uploads failed: {'; '.join(failed_uploads)}"
            if not uploaded_files:  # All uploads failed
                raise Exception(error_msg)
            else:  # Partial failure
                logger.warning(error_msg)

        return uploaded_files

    except Exception as e:
        # Cleanup any successfully uploaded files on total failure
        for uploaded_file in uploaded_files:
            try:
                cleanup_gemini_file(client, uploaded_file.name)
            except:
                pass
        raise


def cleanup_gemini_files_batch(client: genai.Client, file_names: List[str]):
    """
    Delete multiple files from Gemini File API to free up storage.

    Args:
        client: Configured Gemini client
        file_names: List of file names to delete
    """
    for file_name in file_names:
        try:
            client.files.delete(name=file_name)
            logger.info(f"Cleaned up Gemini file: {file_name}")
        except Exception as e:
            logger.warning(f"Failed to cleanup Gemini file {file_name}: {str(e)}")


def cleanup_gemini_file(client: genai.Client, file_name: str):
    """
    Delete a file from Gemini File API to free up storage.

    Args:
        client: Configured Gemini client
        file_name: Name of the file to delete
    """
    try:
        client.files.delete(name=file_name)
        logger.info(f"Cleaned up Gemini file: {file_name}")
    except Exception as e:
        logger.warning(f"Failed to cleanup Gemini file {file_name}: {str(e)}")
