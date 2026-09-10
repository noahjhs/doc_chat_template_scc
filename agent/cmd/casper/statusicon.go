package main

import (
	"bytes"
	"image"
	"image/color"
	"image/png"
	"math"
)

// dotIconPNG renders a small filled circle (transparent background) as PNG
// bytes, for the status-bar menu's "service is running/paused" indicator
// (see daemon.go's applyState) -- generated in memory rather than shipped
// as an asset file, since it's just a single flat color circle.
func dotIconPNG(c color.RGBA) []byte {
	const size = 16
	img := image.NewRGBA(image.Rect(0, 0, size, size))
	cx, cy := float64(size-1)/2, float64(size-1)/2
	radius := float64(size)/2 - 1
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			dx, dy := float64(x)-cx, float64(y)-cy
			if math.Hypot(dx, dy) <= radius {
				img.Set(x, y, c)
			}
		}
	}
	var buf bytes.Buffer
	_ = png.Encode(&buf, img)
	return buf.Bytes()
}

var (
	// Applied via setStatusDotIcon (statusicon_darwin.go), not
	// MenuItem.SetIcon/SetTemplateIcon -- a template image gets forced to
	// monochrome by macOS to match the menu bar's light/dark appearance,
	// which would defeat the point of a green/gray status color; a plain
	// (non-template) NSImage keeps the real color regardless.
	greenDotIcon = dotIconPNG(color.RGBA{R: 0x2E, G: 0xC7, B: 0x5F, A: 0xFF})
	grayDotIcon  = dotIconPNG(color.RGBA{R: 0x9E, G: 0x9E, B: 0x9E, A: 0xFF})
)
