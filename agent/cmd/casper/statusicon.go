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
//
// The canvas is wider than the circle itself (flush left, transparent to
// its right) rather than exactly circle-sized -- NSMenuItem's state column
// (see statusicon_darwin.go) reserves space based on the image's own
// reported size, not just its visible content, so the extra blank width is
// what actually adds breathing room before the item's title text starts;
// asked for directly after the dot and title looked correctly aligned but
// a bit cramped together. dotCanvasWidthPt/dotCanvasHeightPt below are the
// logical point size statusicon_darwin.go's setSize call must match --
// keep them in sync if this canvas's proportions ever change.
const (
	dotDiameter       = 16
	dotCanvasWidth    = 24
	dotCanvasWidthPt  = 18
	dotCanvasHeightPt = 12
)

func dotIconPNG(c color.RGBA) []byte {
	img := image.NewRGBA(image.Rect(0, 0, dotCanvasWidth, dotDiameter))
	cx, cy := float64(dotDiameter-1)/2, float64(dotDiameter-1)/2
	radius := float64(dotDiameter)/2 - 1
	for y := 0; y < dotDiameter; y++ {
		for x := 0; x < dotDiameter; x++ {
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
