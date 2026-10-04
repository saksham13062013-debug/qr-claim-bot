FROM node:20-bookworm-slim
ENV NODE_ENV=production
WORKDIR /app
COPY package*.json ./
RUN npm install --omit=dev
COPY . .
RUN mkdir -p /app/data /app/uploads && chown -R node:node /app
USER node
EXPOSE 3000
CMD ["npm", "start"]
