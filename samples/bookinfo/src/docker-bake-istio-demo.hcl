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
    name = "examples-bookinfo-ratings-v3"
    args = {
      service_version = "v3"
    }
    source = "ratings"
  },
  {
    name = "examples-bookinfo-ratings-v-50percent-internal-server-error-500"
    args = {
      service_version = "v-50percent-internal-server-error-500"
    }
    source = "ratings"
  },
  // Details
  {
    name = "examples-bookinfo-details-v3"
    args = {
      service_version              = "v3"
      enable_external_book_service = true
    }
    source = "details"
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