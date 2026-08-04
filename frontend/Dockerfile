# 前端镜像：两阶段构建（node 构建 → nginx 托管静态文件）
# 镜像源用国内加速（网络正常可改回 node:22-alpine / nginx:alpine）
# 阶段一：构建
FROM docker.m.daocloud.io/library/node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# 阶段二：运行时（只留静态文件 + nginx，镜像小）
FROM docker.m.daocloud.io/library/nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
