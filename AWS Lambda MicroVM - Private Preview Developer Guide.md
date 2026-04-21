

# AWS Lambda MicroVM Developer Guide

Copyright 2026 Amazon Web Services, Inc and/or its affiliates. All rights reserved. 

Amazon’s trademarks and trade dress may not be used in connection with any product or service that is not Amazon’s, in any manner that is likely to cause confusion among customers, or in any manner that disparages or discredits Amazon. All other trademarks not owned by Amazon are the property of their respective owners, who may or may not be affiliated with, connected to, or sponsored by Amazon.

**Note**: this guide is a pre-release document. The service behavior described herein may change from time-to-time prior to release of the feature. This confidential guide and the confidential information provided herein are provided under the **terms and conditions of your nondisclosure agreement (NDA)**, your Customer Agreement, and any other agreements governing your receipt of confidential information from AWS.

# **Introduction**

A MicroVM is an AWS Lambda compute resource designed for use cases that require general-purpose compute environments with the strong isolation and rapid scaling of AWS Lambda. Powered by [Firecracker virtualization](https://aws.amazon.com/blogs/opensource/firecracker-open-source-secure-fast-microvm-serverless/), AWS Lambda MicroVMs give developers the ability to run any application within secure, lightweight execution environments with control over request routing, environment lifecycles, and state retention. 

Developers can launch, suspend, and terminate MicroVMs while maintaining the serverless benefits of built-in availability, responsive scaling, environment patching, and pay-per-use pricing. MicroVMs support up-to 8 hours of execution time, provide full operating system access, and offer fine-grained control over ingress and egress networking. 

# **When to use Lambda MicroVMs**

MicroVMs are purpose-built for use cases that execute user or AI-generated code and require execution environments that offer strong isolation, rapid launch and resume latency, and give developers control over environment lifecycle and state. Typical use cases include:

* Interactive code playgrounds and development environments  
* Ephemeral sandboxes to execute AI generated code    
* Data analytics platforms including Jupyter notebooks and ephemeral data processing workloads that execute user-supplied scripts  
* Security scanning and vulnerability assessment tools that need isolated execution environments  
* Game servers that execute user-supplied scripts  
* Multi-tenant CI/CD task executors

# **Supported features for private preview** 

The following features/constraints apply during Lambda MicroVMs private preview.

1. Only ARM64 CPU architecture is supported.

2. Snapshot optimization (see below) is required.

3. If referencing Amazon ECR images in your Dockerfile, ensure that the repository is accessible over the public Internet. For private ECR repositories, see next bullet point.

4. VPC connectivity is not supported. (**Note**: Notify your account team if this capability is a hard blocker for your use case. )

# **Key concepts**

Understanding these core concepts is essential for working effectively with Lambda MicroVMs:

* **MicroVM**: A serverless ephemeral compute environment (maximum execution duration of 8 hours) that combines the isolation of a VM with the resource efficiency of containers. A MicroVM runs an Amazon Linux operating system environment and supports (optional) ingress connectivity over a service-provided HTTPS endpoint to user-configurable ports. This enables developers to build applications using popular TCP-based connectivity protocols (WebSockets and gRPC are supported) with programming languages and frameworks of their choosing. A MicroVM can be suspended after remaining idle for a configurable time(up-to 8 hours), automatically resuming execution in response to incoming traffic.

* **MicroVM Image**: An artifact that contains the runtime environment, application code, and supporting programs to execute within a MicroVM. These components are developer-owned and can be supplied using a zip package stored in your AWS S3 buckets. Developers can create a new version of a MicroVM Image to update their runtime environment, application code, or supporting programs. Lambda publishes a managed MicroVM Image (consisting of an Amazon Linux operating system and service components) which serves as the base image for all MicroVM Images.

* **MicroVM Image Snapshots:** Lambda MicroVM Images are created by taking [Firecracker snapshots](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md#about-microvm-snapshotting) of the initialized disk and memory state of your application. During the image build phase, Lambda runs your Dockerfile or retrieves your container image (not supported for preview). 

  If you enable snapshot optimization (**on by default for preview**), Lambda also stores the memory state of running applications within your MicroVM Image. This includes the state of any applications or background processes launched via the ENTRYPOINT or CMD commands in your Dockerfile. With this optimization, any MicroVMs that are subsequently launched from this image do not need to perform application startup again. Instead, they resume execution from the pre-initialized snapshotted state of running applications launched by your Dockerfile. This enables you to launch MicroVMs – with applications already initialized – within one second for every 500 MB of snapshotted state. 

  (Note: Launch speed depends on the size of snapshotted state used. For e.g. snapshot size is 2 GB, but only 500 MB of it is used by your running applications, you can still experience 1s launch speed. Test your application to determine achieved launch speeds).

* **Network Connectors**: Connectors define the configuration for inbound and outbound network access from a MicroVM. You can define an ingress network connector to enable inbound connectivity on specific ports and an egress network connector to manage outbound connectivity. Lambda MicroVMs supports TLS/SSL encrypted traffic inbound traffic on all ports except system reserved ones (0-1024; 80 and 443 supported) and supports outbound connectivity to the public Internet, customers’ VPC, or isolated networking mode. **NOTE:** During preview, VPC egress network connectivity is not supported using network connector. If using VPC egress is a hard blocker for your use case, contact your account team providing your VPC ID, subnet list, and security group list for outbound network traffic.

**Getting started**

## **Console Access**

Login to your AWS account, choose the US East 2 (Ohio) Region and navigate to the AWS Lambda console. Once preview access is enabled for your account, you should be able to view Lambda MicroVMs from the Lambda landing page (you may need to expand the left-hand side menu). You can use the Console to perform all the actions described below. In this guide, we demonstrate the use of the Lambda MicroVMs CLI. 

## **Pre-requisites**

Create an S3 bucket in the US-East-2 region. Use this bucket to store your application artifacts for use with Lambda MicroVMs. 

**CLI Setup** 

To use the CLI: 

1. Download the service model json provided in the attached zip (filename: lambdamicrovms-2025-09-09.json).

2. Run the following commands:

aws configure add-model \--service-model file://\<Path\>/\<To\>/lambdamicrovms-2025-09-09.json \--service-name lambda-microvms

3. You can verify the setup was successful with the following:  
   

aws lambda-microvms list-micro-vm-images \\  
  \--region us-east-2 \\  
  \--endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev

**Note:** Region and Endpoint are required for all CLI commands. The endpoint URL shown above is an internal endpoint only used during private preview.

**Sample applications**

Refer to the sample Python REPL, Flask web app, and Node JS GRPC apps provided with this guide.

##  **Creating your first MicroVM Image** 

To start using Lambda MicroVMs, you first create a MicroVM Image. MicroVM Images contain the runtime environment, application code, and supporting programs (for e.g. observability agents) for your application. When you create a Lambda MicroVM Image, Lambda retrieves your packaged software artifacts, executes any installation scripts, and takes a snapshot of the disk and memory state after initializing your software artifacts. This enables you to achieve as fast as sub-second startup times for latency-sensitive applications that take several seconds or longer to initialize (See note on **Compatibility considerations for snapshots**).

Note: During preview, updating a MicroVM Image with new software artifacts is not supported. Instead, you must create a new MicroVM Image.

### **Installing your software artifacts**

Lambda MicroVMs installs your software artifacts and initializes your application by executing the instructions supplied in your Dockerfile. The underlying operating system is Amazon Linux 2023\. The following example shows a sample Dockerfile that installs a Node.js application and exposes it on port 8080\. 

\# Example Dockerfile for a NodeJS App

\# For preview, the following constraints apply to base container images:  
\# 1\. Base images must be accessible over the public Internet  
\# 2\. Base images must be compatible with the ARM64 CPU architecture  
FROM node:24-alpine

\# Install build dependencies  
RUN apk add \--no-cache python3 g++ make  
WORKDIR /app

\# Build and copy our app to the working directory  
COPY package\*.json ./  
RUN npm install \--production  
COPY app.js .  
COPY templates/ templates/

\# Expose any port(s) your application runs on  
EXPOSE 8080

\# Start our application  
CMD \["node", "app.js"\]

### **Packaging your software artifacts**

Package your software artifacts into a zip file and upload it to your S3 bucket. The following script sample demonstrates how:

\# Deploy.sh – Run from the root directory of your app  
S3\_BUCKET\="\<YOUR\_BUCKET\_HERE\>" \# Must be in us-east-2  
zip \-r app.zip .  
echo "Package created: $(ls \-lh app.zip)"  
TIMESTAMP\=$(date \+%Y%m%d-%H%M%S)  
S3\_KEY\="deployments/app-deployment-${TIMESTAMP}.zip"  
aws s3 cp app.zip s3://${S3\_BUCKET}/${S3\_KEY} \--region us-east-2  
echo "Uploaded to s3://${S3\_BUCKET}/${S3\_KEY}"

NOTE: Ensure that the bucket is in the US East 2 (Ohio) Region.

### **IAM permissions**

Lambda requires an IAM role to create your MicroVM image. This role is used to download the supplied code artifact and to push logs to Amazon CloudWatch while building the MicroVM Image. Include permissions required to execute your MicroVM’s build step, get/put from Amazon S3, and emit logs to Amazon CloudWatch.

{  
    "Version": "2012-10-17",  
    "Statement": \[  
        {  
            "Effect": "Allow",  
            "Action": \[  
                "logs:\*"  
            \],  
            "Resource: "arn:aws:logs:\*:\*:\*"  
   	 },  
 {  
            "Effect": "Allow",  
            "Action": \[  
                "s3:GetObject",  
         "s3:PutObject"  
            \],  
            "Resource: "arn:aws:s3:::\*"  
    	 },  
      \]

}

For preview, your the IAM role requires the following trust policy to enable the service to assume it: 

{  
    "Version": "2012-10-17",  
    "Statement": \[  
        {  
            "Effect": "Allow",  
            "Principal": {  
                "Service": "lambda-microvms-private-preview.amazonaws.com"  
            },  
            "Action": \[  
                "sts:AssumeRole",  
                "sts:TagSession"  
            \]  
        }  
    \]  
}

### 

Your role will need the following IAM permissions (plus, any other permissions required by your app):

* **s3:GetObject** – required to download your zip artifact

* **ecr:GetAuthorizationToken** – required if your Dockerfile references a private ECR image (ie. FROM \<private ECR image\>)

* **logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents** – for shipping application stdout logs to CloudWatch

### **Creating your MicroVM Image:**

Once you have completed the prior steps, use the following CLI command to create your MicroVM Image:

aws lambda-microvms create-micro-vm-image \\  
  \--code-artifact uri=\<path/to/s3/artifact.zip\> \--name \<mVM\_image\_name\> \\  
  \--base-micro-vm-image-arn arn:aws:lambda:::microvm-image:lambda-microvms-al2023-1 \\  
  \--build-role-arn \<IAM role ARN\>  
  \--region us-east-2 \--endpoint [https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev](https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev)

**NOTE:** To debug issues during MicroVM Image creation, view the image creation logs in CloudWatch. These logs are organized under the path: /aws/lambda/microvms/\<image-name\>.  

###   

### **Compatibility considerations for MicroVM Image snapshots**

Lambda uses a single snapshot as the initial state for all MicroVMs launched from that MicroVM Image. If the code you execute when building your MicroVM Image uses any of the following, you might need to make some changes before using Lambda MicroVMs’ snapshotting capability:

- **Uniqueness:** If your code generates unique content that is included in the MicroVM Image, then the content might not be unique when it is reused across MicroVMs. To maintain uniqueness, you must generate unique content **after** launching a MicroVM. This includes unique IDs, unique secrets, and entropy that's used to generate pseudorandomness. To learn how to restore uniqueness, see **Handling Uniqueness with Lambda MicroVMs**.

- **Network Connections:** The state of connections established when your MicroVM Image is being built isn't guaranteed when Lambda resumes MicroVMs from it. Validate the state of your network connections and re-establish them as necessary. In most cases, network connections that an AWS SDK establishes automatically resume. For other connections, review **Networking best practices with Lambda MicroVMs.**

  ### 

## **Launching a MicroVM** 

Once your MicroVM Image is ready, you can start creating and using MicroVMs. Lambda launches a new MicroVM from the specified MicroVM Image, assigns it a unique ID, and creates a unique HTTP/2-compatible endpoint you can use to connect to your MicroVM. Lambda automatically provisions the required resources used by your running MicroVM, up-to 4 vCPUs and 8 GB of memory. You can use the AWS Lambda CLI or SDK to launch a MicroVM, as shown in the following example:

aws lambda-microvms launch-micro-vm \\  
  \--micro-vm-image-arn \<Your Image ARN\> \\  
  \--micro-vm-image-version 1.0 \\  
  \--ingress-network-connectors "arn:aws:lambda:::network-connector:aws-network-connector:ALL\_INGRESS" \\ \# optional  
  \--egress-network-connectors "arn:aws:lambda:::network-connector:aws-network-connector:INTERNET\_EGRESS" \\ \# optional  
  \--idle-policy autoResumeEnabled=true,maxIdleDurationSeconds=900,suspendedDurationSeconds=300 \\  
  \--region us-east-2 \\  
  \--endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev

## **Using a MicroVM** 

Once you launch a MicroVM you can connect to any running applications within it using its dedicated URL endpoint. For preview, ingress to all MicroVMs is via a fixed URL endpoint (shown below). At GA, we plan to expose a separate URL endpoint for each MicroVM. 

To connect to a MicroVM, you must use an authentication token. Generate this auth token using the CLI or SDK and include in your request using the HTTP Header X-aws-proxy-auth (see **Authentication Tokens**). By default, Lambda MicroVMs supports inbound traffic on ports 443, routing it to port 8080 within the MicroVM. You can optionally use the X-aws-proxy-port HTTP Header to specify which port to forward your request.

Curl:

curl 'https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev' \\  
  \-H 'X-aws-proxy-auth: \<TOKEN\>'  
  \-H 'X-aws-proxy-port: \<PORT\>' \# Optional

NodeJS:

const response \= await fetch('https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev', {   
  headers: {  
    'X-aws-proxy-auth': '\<TOKEN\>',  
    'X-aws-proxy-port': '\<PORT\>', // Optional  
  }}  
);  
console.log(response);

Python:

import requests  
response \= requests.get('https://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev',   
  headers\={'X-aws-proxy-auth': '\<TOKEN\>'})  
print(response)

Outbound connectivity to the public Internet is supported without any additional configuration. To override default behavior, see the section on **Network Connectivity.**

**Browser-based WebSocket Connections**

Web Browsers do not support passing arbitrary headers (such as \`X-aws-proxy-auth\`) when establishing WebSocket connections. As a result, auth token and port values need to be supplied via WebSocket Subprotocol header (\`Sec-Websocket-Protocol\`). The sub-protocol values take the format:

* Base Protocol: \`lambda-microvms\`  
* Auth Token Protocol: \`lambda-microvms.authentication.\<auth-token\>\`  
* Port Protocol: \`lambda-microvms.port.\<port-number\>\`

The base protocol (\`lambda-microvms\`) is always required when supplying auth token or port via the sub-protocol header. If no additional sub-protocols are supplied in addition to the Lambda MicroVM specific protocols, then the base protocol (\`lambda-microvms\`) will be returned in the (\`Sec-Websocket-Protocol\`) header as the selected protocol.

**Note:** All Lambda MicroVM specific WebSocket sub-protocols are removed from the request before forwarding it to the MicroVM.

**Example**  
const protocols \= \[  
    "lambda-microvms",  
    "lambda-microvms.authentication.\<auth-token-here\>",  
    "lambda-microvms.port.9000"  
\];

const ws \= new WebSocket(endpoint, protocols);

# **Operating MicroVMs**

## **Connecting via shell**

You can access running MicroVMs through multiple connection methods. If using the AWS Console, navigate to the MicroVMs page, select your MicroVM, and choose ‘Connect’.

## **Logging and monitoring**

Application logs are automatically streamed to **CloudWatch**. Log groups organized under /aws/lambda/microvms/\<image-name\> for easy filtering.  Service provided CloudWatch metrics are not supported for preview. Emit custom application metrics using CloudWatch API. 

## **Note on MicroVM shell access during preview**

During preview, accessing the MicroVM shell places you on the MicroVM host OS, within the root directory. To access the container within which your application runs do the following. First, find your application container using the following command:

ctr task ls

Then, take the output container ID, and shell into your applications container:

ctr task exec \-t \--exec-id shell \<\<container\_id\>\> /bin/sh

## **MicroVM lifecycle hooks** You can optionally implement lifecycle hooks that are executed at various points during the lifecycle of a Lambda MicroVM. Implement these hooks as API actions (bound on port 9000) within your Lambda MicroVM. The OpenAPI spec can be found in the Zip package provided. The following hooks are supported: 

\#\#  
\# Invoked during MicroVM Image build to determine when your application  
\# has successfully started. Perform any post-completion checks to validate  \# application behavior using this hook.

\# Timeout: 60m

POST /aws/lambda-microvms/runtime/beta/v1/ready

\#\#  
\# Invoked when your MicroVM has been successfully launched (resumed from a \# snapshot). Use this hook to perform any health checks or validation  
\# steps to ensure your app is healthy, or to reset any unique content such  
\# as randomly generated request IDs).

\# Timeout: 60m

POST /aws/lambda-microvms/runtime/beta/v1/launch

\#\#  
\# Invoked just before suspending your MicroVM. Use this hook to perform  
\# any pre-suspend steps, for example cleaning up resources including   
\# open connections. You can also pre-load dependencies to optimize  
\# startup latency. More details [here](https://docs.aws.amazon.com/lambda/latest/dg/snapstart-best-practices.html).  
\# After a suspend, a microVM can either be launched (if this suspend was   
\# during image build), or resumed (if this suspend was after a launch).

\# Timeout: 120s  
	  
POST /aws/lambda-microvms/runtime/beta/v1/suspend  
\#\#  
\# Invoked after your MicroVM has been resumed from an in-place suspend  
\# (not from a launch). Use this hook to recreate any resources that were  
\# cleaned up prior to suspend, such as open network connections.

\# Timeout: 120s

POST /aws/lambda-microvms/runtime/beta/v1/resume

\#\#  
\# Invoked before terminating your MicroVM. Use this hook to perform   
\# any actions prior to cleanup, such as flushing pending data.

\# Timeout: 60s

POST /aws/lambda-microvms/runtime/beta/v1/terminate

# **Permissions and Security**

## **IAM roles and policies**

Lambda MicroVMs supports the use of two IAM roles to separate build-time and execution-time permissions. The build-time role, supplied when creating a MicroVM image, is required. It is used to download the supplied code artifact and push logs to CloudWatch while building the MicroVM Image. The build role can be used to customize your applications build as well.

The execution role is optional. It is supplied when calling Launch MicroVM and used to offload application logs from the MicroVM to CloudWatch. Once launched, this role can also be used by your application.

Roles will need the following trust policy to allow our service to assume them:

{  
    "Version": "2012-10-17",  
    "Statement": \[  
        {  
            "Effect": "Allow",  
            "Principal": {  
                "Service": "lambda-microvms-private-preview.amazonaws.com"  
            },  
            "Action": \[  
                "sts:AssumeRole",  
                "sts:TagSession"  
            \]  
        }  
    \]  
}  
 

Your role will need the following IAM permissions (plus, any other permissions required by your application):

* **logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents** – to transmit application stdout logs to CloudWatch

**Authentication tokens**

After Launching a MicroVM, auth tokens can be generated using the following CLI command:  
aws lambda-microvms generate-micro-vm-auth-token \\  
  \--micro-vm-id \<Your MicroVM ID\> \\  
  \--expiration-minutes 30 \\  
  \--region us-east-2 \\  
  \--endpoint https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev

This auth token can then be used to send requests to your MicroVM by following the steps in **Using a MicroVM**.

# **Network connectivity**

## **Inbound connectivity**

Lambda MicroVMs support inbound network connectivity through ingress network connector resources. Ingress connectors are optional and connectivity to ports 80 and 443 is supported by default. To customize this, create one or more ingress connector resources defining the ports to support inbound connectivity on:

Lambda MicroVMs support inbound connectivity over any TCP-based protocol, including HTTP, HTTP/2 (for bi-directional request/response streaming), gRPC, and WebSockets. The following examples demonstrate this.

Example WebSocket Connection:

const url \= "wss://cell01.us-east-2.gamma.arp.kepler-analytics.aws.dev/\<PathToWebsocketServer\>";

/\*\*  
\* IMPORTANT: browsers send subprotocols as a list, not multiple headers.  
\* If not using a browser, these can also be passed as headers.  
\*/   
const protocols \= \[  
"lambda-microvms.authentication.\<AuthToken\>", // "X-aws-proxy-auth": \<AuthToken\>  
"lambda-microvms.port.\<PortOfWebsocketServer\>", // "X-aws-proxy-port": \<PortOfWebsocketServer\>  
"lambda-microvms", // Not needed when using headers  
\];

Const ws \= new WebSocket(url, protocols);  
ws.onopen \= () \=\> {}  
ws.onerror \= (event) \=\> {}  
ws.onmessage \= (event) \=\> {}  
ws.onclose \= (event) \=\> {}

You can also connect to a running Lambda MicroVM using the ‘wscat’ utility. This is useful during troubleshooting, for instance to execute commands or view logs stored on the MicroVM. For more details about logging, refer to the section on **Logging and Monitoring**. 

**SSL Termination**   
Traffic sent from your client applications to your Lambda MicroVM always uses SSL encryption. SSL termination takes place on a proxy endpoint process that resides within your MicroVM. You can also encrypt traffic between this endpoint to your webserver process within your MicroVM. In this scenario, Lambda MicroVMs automatically detects the use of SSL and performs SSL termination on your webserver process.   
   
**HTTP/2 Support**   
Lambda MicroVMs support HTTP/2 connections natively. HTTP/2 can be used in two scenarios: 

* **SSL endpoints**: When connecting to a server with SSL, Lambda MicroVMs negotiate HTTP/2 via ALPN (Application-Layer Protocol Negotiation) during the TLS handshake, falling back to HTTP/1.1 if HTTP/2 is not supported. 

* **Plaintext with explicit header**: When the request includes X-aws-proxy-force-h2: true, Lambda MicroVMs will use HTTP/2 regardless of the endpoint protocol. 

## **Outbound connectivity**

MicroVMs provide flexible outbound connectivity options. By default, connectivity to public Internet is supported. You can optionally disable outbound network access entirely or configure VPC connectivity. 

**NOTE:** During preview, the following constraints apply: 

* VPC egress network connectivity is not supported. If this is a blocker for your use case, contact your account team providing your VPC ID, subnet list, and security group list. 

* Logs are not automatically transmitted to CloudWatch when using the **NO\_EGRESS** option. To access logs, connect to your MicroVM using ‘wscat’ and view the logs directly. Logs are only retained until your MicroVM terminates in this mode.

# **MicroVM lifecycle management**

## **Suspending MicroVMs**

Suspend idle MicroVMs to reduce costs while preserving application state. You can also configure a lifecycle policy that automatically suspends an idle MicroVM after a defined duration. Suspended MicroVMs retain their disk and memory state, enabling you to resume execution where you left off.

**NOTE**: Idle time is measured by traffic flowing through the MicroVM’s endpoint URL. For asynchronous applications that do not actively receive/send traffic to clients connecting over their endpoint URL, disable automatic suspension or configure a suitable idle duration to avoid unintended suspension. 

## **Resuming MicroVMs**

Resume suspended MicroVMs quickly to restore application execution. You can resume a suspended MicroVM by calling the ‘resumeMicroVM’ API or by sending traffic to your MicroVM’s URL endpoint. Resuming a MicroVM is an asynchronous operation that can take a few seconds for large MicroVMs (1s per 500 MB of suspended state – includes disk and memory). You must enable auto-resume to leverage this capability. 

The following example shows an example with auto-resume enabled, a maximum idle duration of 900s after which the MicroVM is suspended, and a maximum suspended duration of 300s after which the MicroVM is terminated.

aws lambda-microvms launch-micro-vm \\  
  \--micro-vm-image-arn \<Your Image ARN\> \\  
  ... \# other parameters  
  \--idle-policy autoResumeEnabled=true,maxIdleDurationSeconds=900,suspendedDurationSeconds=300 \\  
  \--region us-east-2 \\

  \--endpoint [https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev](https://cell01.us-east-2.gamma.fe.kepler-analytics.aws.dev)

**Service quotas and limits**

Lambda MicroVMs operates within the following service limits. During preview, these limits are fixed.

| Quota | Limit |
| ----- | ----- |
| Concurrent MicroVMs | 1000 (per account) |
| Resource limit per MicroVM | 4 vCPUs / 8 GB memory / 32 GB disk |
| MicroVM Images per account | 1000 |
| Zip Artifact size limit | 32 GB (same as disk space limit) |

# 

# **Common Errors and Troubleshooting**

* **S3\_ACCESS\_DENIED:** The IAM role specified doesn't have permissions to retrieve your source S3 artifact.

* **S3\_NO\_SUCH\_KEY:** The artifact key does not exist in the specified S3 bucket. Verify that the S3 path is correct.

* **S3\_NO\_SUCH\_BUCKET:** The artifact S3 bucket does not exist. Check that the bucket name is correct and that the bucket has been created.

* **S3\_INVALID\_OBJECT:** The artifact is not a standard S3 object. This occurs when the object is stored in Glacier, uses Intelligent-Tiering, or another storage class that isn't directly accessible.

* **S3\_CROSS\_REGION\_ACCESS\_DENIED:** The artifact is in a different AWS region than MicroVM Image being created. Ensure your artifact is in the same region.

* **ARCHIVE\_DOCKERFILE\_NOT\_FOUND:** The zip archive specified during MicroVM Image creation is missing a Dockerfile in the root directory. During preview, archives must include a Dockerfile.

* **ARCHIVE\_INVALID:** The archive file is invalid or corrupted. This may occur if the file is not a valid ZIP archive or has been corrupted during upload.

* **CONTAINER\_BUILD\_FAILED:** The container build process failed. Common causes include invalid Dockerfile instructions, missing files referenced in COPY commands, or syntax errors in the Dockerfile. Please debug locally.

* **DISK\_STORAGE\_FULL:** The MicroVM has run out of storage space during the build process. **Please reach out to support for assistance with storage capacity.**

* **INTERNAL\_PLATFORM\_ERROR:** An internal platform error occurred that doesn't fit into the other error categories. If you encounter this error repeatedly, please contact support with your build details.

# **Handling uniqueness with Lambda MicroVMs snapshots**

With snapshot optimization (enabled for private preview), Lambda uses a single initialized MicroVM Image snapshot to launch multiple MicroVMs. If the code you execute when building your MicroVM Image generates unique content that is included in the snapshot, then the content might not be unique when it is reused across MicroVMs launched from the same image. To maintain uniqueness when using this capability, you must generate unique content when launching a MicroVM, rather than during MicroVM Image building. This includes unique IDs, unique secrets, and entropy that's used to generate pseudorandomness.

## **Use cryptographically secure pseudorandom number generators (CSPRNGs)**

If your application depends on randomness, we recommend that you use cryptographically secure random number generators (CSPRNGs). Software that always gets random numbers from /dev/random or /dev/urandom also maintains randomness when used with MicroVM Image snapshots. Below, we note the CSPRNGs supported by popular programming languages. Ensure your Lambda MicroVMs applications use the below-mentioned CSPRNGs for the languages listed below:

- **Java:** java.security.SecureRandom  
- **Python:** random.SystemRandom  
- **Dotnet:** System.Security.Cryptography.RandomNumberGenerator  
- **Node.js:** crypto.randomBytes

**NOTE:** Lambda MicroVMs runtime environment includes a version of the OpenSSL library that is compatible with MicroVMs resumed from a MicroVM Image snapshot. Ensure that you use this version of OpenSSL with Lambda MicroVMs.