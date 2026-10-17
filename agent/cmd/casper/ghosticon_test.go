package main

import (
	"bytes"
	"encoding/binary"
	"image/color"
	"image/png"
	"testing"
)

func TestGhostICOIsAnIconHoldingAColouredGhost(t *testing.T) {
	ico := ghostICO(color.RGBA{R: 0x2E, G: 0xC7, B: 0x5F, A: 0xFF})
	if binary.LittleEndian.Uint16(ico[2:]) != 1 || binary.LittleEndian.Uint16(ico[4:]) != 1 {
		t.Fatal("not a one-image icon file")
	}
	size, offset := binary.LittleEndian.Uint32(ico[14:]), binary.LittleEndian.Uint32(ico[18:])
	if int(offset+size) != len(ico) {
		t.Fatalf("directory says %d+%d, file is %d bytes", offset, size, len(ico))
	}
	img, err := png.Decode(bytes.NewReader(ico[offset:]))
	if err != nil {
		t.Fatal(err)
	}
	if _, _, _, a := img.At(16, 20).RGBA(); a == 0 {
		t.Fatal("the body should be filled")
	}
	if _, _, _, a := img.At(1, 1).RGBA(); a != 0 {
		t.Fatal("the corner should be clear")
	}
	if bytes.Equal(greenGhostICO, grayGhostICO) {
		t.Fatal("running and paused must differ")
	}
}
