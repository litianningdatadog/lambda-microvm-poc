# Private Release Usage for Python and AWS CLI v1

1. Unzip the `Boto3CliV1Artifacts.zip` preview build artifact
2. Enter the directory: `cd ./Boto3CliV1Artifacts`
3. Activate the Python virtual environment appropriate for your version of Python:
   ```
   python3 -m venv python-sdk-test && source python-sdk-test/bin/activate
   ```
4. Install the dependencies:
   ```
   python3 -m pip install botocore-<version>-py3-none-any.whl
   ```
5. Install **s3transfer** if it exists in the archive:
   ```
   python3 -m pip install s3transfer-<version>-py3-none-any.whl
   ```
6. For further instructions on installing the Python SDK, follow [boto3](#python-boto3) or for CLI installation, see [AWS CLI v1](#aws-cli-v1).

If you encounter problems or wish to provide feedback, notify the point of contact in the emailed instructions for this private release.

## Python (boto3)

For more information on how to get started with the AWS SDK for Python, [see the documentation](https://docs.aws.amazon.com/frauddetector/latest/ug/getting-started-python.html).

1. Install the Python SDK:
   ```
   python3 -m pip install boto3-<version>-py3-none-any.whl
   ```
2. Verify **boto3** installed successfully by running the following command and confirming the version printed is the same as the wheels file:
   ```
   python3 -c "import boto3; print(boto3.__version__)"
   ```

## AWS CLI v1

For more information on how to get started with AWS CLI v1, [see the documentation](https://docs.aws.amazon.com/cli/v1/userguide/install-linux.html).

1. Install the AWS CLI:
   ```
   python3 -m pip install awscli-<version>-py3-none-any.whl
   ```
2. Ensure the shell is pointing to the preview build aws binary and not a globally installed one: `which aws`
3. Verify the preview build is working by running the following command and confirming the version printed is the same as the wheels file: `aws --version`
