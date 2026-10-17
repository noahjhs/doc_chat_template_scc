package main

import (
	"bytes"
	"encoding/binary"
	"image"
	"image/color"
	"image/png"
	"math"
)

// ghostICO draws the tray icon: a ghost in one flat colour (green when Casper
// is running, grey when paused) with dark eyes, so it reads on a light or a
// dark taskbar. Windows wants an .ico; a PNG inside the container is fine
// from Vista on.
func ghostICO(fill color.RGBA) []byte {
	const size = 32
	img := image.NewRGBA(image.Rect(0, 0, size, size))
	eye := color.RGBA{R: 0x20, G: 0x24, B: 0x28, A: 0xFF}
	const cx, top, radius = 15.5, 3.0, 11.5
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			fx, fy := float64(x)+0.5, float64(y)+0.5
			in := false
			switch {
			case fy < top+radius: // rounded head
				in = math.Hypot(fx-cx, fy-(top+radius)) <= radius
			case fy < 27: // straight sides
				in = math.Abs(fx-cx) <= radius
			default: // wavy hem: three scallops
				wave := 27 + 2.5*math.Sin((fx-(cx-radius))/(2*radius)*3*2*math.Pi)
				in = math.Abs(fx-cx) <= radius && fy < wave+1.5
			}
			if in {
				img.SetRGBA(x, y, fill)
			}
		}
	}
	for _, ex := range []float64{11.5, 19.5} {
		for y := 11; y < 17; y++ {
			for x := int(ex) - 2; x <= int(ex)+2; x++ {
				if math.Hypot(float64(x)+0.5-ex-0.5, (float64(y)+0.5-14)*0.8) <= 2.4 {
					img.SetRGBA(x, y, eye)
				}
			}
		}
	}
	var pngBytes bytes.Buffer
	_ = png.Encode(&pngBytes, img)

	var ico bytes.Buffer
	_ = binary.Write(&ico, binary.LittleEndian, []uint16{0, 1, 1}) // reserved, type icon, one image
	ico.Write([]byte{size, size, 0, 0})
	_ = binary.Write(&ico, binary.LittleEndian, []uint16{1, 32}) // planes, bits per pixel
	_ = binary.Write(&ico, binary.LittleEndian, []uint32{uint32(pngBytes.Len()), 22})
	ico.Write(pngBytes.Bytes())
	return ico.Bytes()
}

var (
	greenGhostICO = ghostICO(color.RGBA{R: 0x2E, G: 0xC7, B: 0x5F, A: 0xFF})
	grayGhostICO  = ghostICO(color.RGBA{R: 0x9E, G: 0x9E, B: 0x9E, A: 0xFF})
)
