"""Synchronous wrapper around Amazon Transcribe for short audio clips."""
import json
import os
import time
import uuid

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("CAIXA_BUCKET")

_s3 = boto3.client("s3", region_name=REGION)
_tr = boto3.client("transcribe", region_name=REGION)


def transcribe(
    audio_bytes: bytes,
    media_format: str = "webm",
    language: str = "pt-BR",
    timeout_s: int = 60,
) -> str:
    if not BUCKET:
        raise RuntimeError("CAIXA_BUCKET env var not set")
    allowed = {"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"}
    if media_format not in allowed:
        media_format = "webm"
    job_name = f"caixa-{uuid.uuid4().hex[:12]}"
    key = f"audio/{job_name}.{media_format}"
    _s3.put_object(Bucket=BUCKET, Key=key, Body=audio_bytes)
    _tr.start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode=language,
        Media={"MediaFileUri": f"s3://{BUCKET}/{key}"},
        MediaFormat=media_format,
        OutputBucketName=BUCKET,
        OutputKey=f"transcripts/{job_name}.json",
    )
    start = time.time()
    try:
        while True:
            job = _tr.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
            status = job["TranscriptionJobStatus"]
            if status == "COMPLETED":
                break
            if status == "FAILED":
                raise RuntimeError(job.get("FailureReason", "transcribe failed"))
            if time.time() - start > timeout_s:
                # P1.12: cancel the in-flight job so we stop billing when the client gave up.
                try:
                    _tr.delete_transcription_job(TranscriptionJobName=job_name)
                except Exception:
                    pass
                raise TimeoutError(f"transcribe timeout {timeout_s}s")
            time.sleep(1.5)
    except BaseException:
        # Any hard interruption → best-effort cleanup of the AWS job.
        try:
            _tr.delete_transcription_job(TranscriptionJobName=job_name)
        except Exception:
            pass
        raise
    obj = _s3.get_object(Bucket=BUCKET, Key=f"transcripts/{job_name}.json")
    payload = json.loads(obj["Body"].read())
    text = payload["results"]["transcripts"][0]["transcript"].strip()
    return text
