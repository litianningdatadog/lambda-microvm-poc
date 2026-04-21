FROM node:24-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install --production

COPY client.js .
COPY server.js .
COPY generate-cert.js .
COPY proto/ proto/

EXPOSE 50051

CMD ["node", "server.js"]