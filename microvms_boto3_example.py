#!/usr/bin/env python3
"""
Script to list AWS Lambda MicroVMs using boto3
"""

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

def list_lambda_microvms():

    try:

        client = boto3.client('lambda-microvms',
            region_name="us-east-2",
            endpoint_url='https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev')

        print(f"Listing Lambda MicroVMs in region: {client.meta.region_name}\n")

        result = client.list_micro_vms()
        for microvm in result['microVMs']:

            print(microvm['microVMId'])

    except NoCredentialsError:
        print("Error: AWS credentials not found. Please configure your credentials.")
    except ClientError as e:
        print(f"Error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")


if __name__ == "__main__":
    list_lambda_microvms()