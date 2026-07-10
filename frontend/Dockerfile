FROM node:20-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Local docker-compose build is the real (non-demo) stack — talks to the
# backend service by its compose network name, not a public URL.
ENV VITE_DEMO_MODE=false
ENV VITE_API_URL=http://localhost:8001
RUN npm run build

FROM node:20-slim
WORKDIR /app
RUN npm install -g serve
COPY --from=build /app/dist ./dist
EXPOSE 3001
CMD ["serve", "-s", "dist", "-l", "3001"]
