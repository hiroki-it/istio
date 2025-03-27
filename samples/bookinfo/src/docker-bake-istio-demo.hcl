variable "TAGS" {
  default = "latest"
}

variable "HUB" {
  default = "localhost:5000"
}

variable "PLATFORMS" {
  default = "linux/amd64,linux/arm64"
}

images = [
  // Productpage
  {
    name   = "examples-bookinfo-productpage-v2"
    source = "productpage"
  },
  // Reviews
  {
    name = "examples-bookinfo-reviews-v4"
    args = {
      service_version = "v4"
      enable_ratings  = true
      star_color      = "red"
    }
    source = "reviews"
  },
  // Ratings
  {
    name = "examples-bookinfo-ratings-v-connection-reset"
    args = {
      service_version = "v-connection-reset"
    }
    source = "ratings"
  },
]

target "default" {
  matrix = {
    item = images
  }
  name    = item.name
  context = "./samples/bookinfo/src/${item.source}"
  tags    = [
    for x in setproduct([HUB], "${split(",", TAGS)}") : join("/${item.name}:", x)
  ]
  args = lookup(item, "args", {})
  platforms = split(",",lookup(item, "platforms", PLATFORMS))
}