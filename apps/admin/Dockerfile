FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
COPY apps/api/package.json ./apps/api/package.json
COPY apps/bot/package.json ./apps/bot/package.json
COPY apps/admin/package.json ./apps/admin/package.json
COPY prisma ./prisma
RUN npm install
COPY . .
RUN npm run build:admin

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/apps/admin/package.json ./apps/admin/package.json
COPY --from=build /app/apps/admin/.next ./apps/admin/.next
COPY --from=build /app/apps/admin/public ./apps/admin/public
EXPOSE 3000
CMD ["npm","--workspace","apps/admin","run","start"]
