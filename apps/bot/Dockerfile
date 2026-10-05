FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
COPY apps/api/package.json ./apps/api/package.json
COPY apps/bot/package.json ./apps/bot/package.json
COPY apps/admin/package.json ./apps/admin/package.json
COPY prisma ./prisma
RUN npm install
COPY . .
RUN npm run build:bot

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/apps/bot/package.json ./apps/bot/package.json
COPY --from=build /app/apps/bot/dist ./apps/bot/dist
EXPOSE 8080
CMD ["node","apps/bot/dist/index.js"]
