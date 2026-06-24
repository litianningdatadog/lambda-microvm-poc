FROM public.ecr.aws/lambda/microvms:al2023-minimal
RUN dnf install -y nodejs && dnf clean all
COPY server.js .
EXPOSE 8080 9000
CMD ["node", "server.js"]
