"""Lambda: check today's sales files in S3 and peek at the first CSV.

Triggered by the Step Functions state machine. Lists objects under
``sales/<today>/``; if a CSV exists, reads it and returns the row count plus the
S3 path the Glue job (later) should clean. Pure application logic — the generator
does NOT write this; the aws-lambda plugin only zips + deploys it.
"""

import boto3
import csv
from io import StringIO
from datetime import datetime

s3 = boto3.client("s3")

# Bucket names are globally unique; this account uses an account-suffixed name.
BUCKET_NAME = "raw-sales-data-936706106045"
PREFIX_ROOT = "sales"


def lambda_handler(event, context):

    today = datetime.utcnow().strftime("%Y-%m-%d")
    prefix = f"{PREFIX_ROOT}/{today}/"

    # List objects under today's folder
    response = s3.list_objects_v2(
        Bucket=BUCKET_NAME,
        Prefix=prefix
    )

    if "Contents" not in response:
        return {
            "statusCode": 200,
            "files_found": False,
            "csv_found": False,
            "row_count": 0,
            "message": f"No files found under {prefix}"
        }

    # Find CSV files
    csv_files = [
        obj["Key"]
        for obj in response["Contents"]
        if obj["Key"].lower().endswith(".csv")
    ]

    if not csv_files:
        return {
            "statusCode": 200,
            "files_found": True,
            "csv_found": False,
            "row_count": 0,
            "message": "Files exist but no CSV found"
        }

    # Read first CSV file
    csv_key = csv_files[0]

    file_obj = s3.get_object(
        Bucket=BUCKET_NAME,
        Key=csv_key
    )

    csv_content = file_obj["Body"].read().decode("utf-8")

    reader = csv.DictReader(StringIO(csv_content))

    rows = list(reader)

    row_count = len(rows)

    return {
        "statusCode": 200,
        "files_found": True,
        "csv_found": True,
        "csv_file": csv_key,
        "row_count": row_count,
        "bucket": BUCKET_NAME,
        "prefix": prefix,
        "s3_path": f"s3://{BUCKET_NAME}/{prefix}"
    }
