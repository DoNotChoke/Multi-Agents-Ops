FROM golang:1.27.3-alpine3.24 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /demo-app .

FROM gcr.io/distroless/static-debian13:nonroot
COPY --from=build /demo-app /demo-app
USER nonroot:nonroot
ENTRYPOINT ["/demo-app"]
