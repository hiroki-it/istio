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
    name   = "examples-bookinfo-productpage-v1"
    source = "productpage"
  },
  {
    name = "examples-bookinfo-productpage-v-flooding"
    args = {
      flood_factor = 100
    }
    source = "productpage"
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
