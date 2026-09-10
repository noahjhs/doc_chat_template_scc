//go:build darwin

package main

/*
#cgo CFLAGS: -x objective-c -fobjc-arc
#cgo LDFLAGS: -framework Cocoa

#include <stdlib.h>
#import <Cocoa/Cocoa.h>

// getlantern/systray's own MenuItem.SetIcon (used by daemon.go's applyState
// before this file existed) sets NSMenuItem.image -- a column of its own,
// to the right of the state/checkmark column, which pushes that item's
// title text further right than every other item's (confirmed directly:
// reported as visibly misaligned). NSMenuItem.onStateImage instead renders
// in that same reserved state/checkmark column every item already gets
// (checkable or not -- Cocoa reserves it uniformly so mixed menus still
// line up), which is what actually lines the dot up with "Run on system
// startup"'s checkmark and keeps this item's title flush with the rest.
// systray doesn't expose onStateImage, and its *MenuItem doesn't expose the
// internal id/tag this package would need to reuse its own find_menu_item
// lookup -- so this reaches into its already-running AppDelegate directly
// instead of forking the library for one property. AppDelegate's "menu"
// ivar has no declared @property, but Objective-C's KVC still resolves a
// plain ivar of that exact name via valueForKey: -- confirmed directly
// against the vendored github.com/getlantern/systray@v1.2.2 sources.
// Best-effort throughout: any lookup failure (a future systray version
// renaming that ivar, e.g.) just leaves the item's default (blank) state
// image in place rather than crashing.
static void setStatusItemStateImage(const char *titlePrefix, const void *pngBytes, int pngLen, double widthPt, double heightPt) {
    dispatch_sync(dispatch_get_main_queue(), ^{
        id delegate = [[NSApplication sharedApplication] delegate];
        if (delegate == nil) {
            return;
        }
        NSMenu *menu = nil;
        @try {
            menu = [delegate valueForKey:@"menu"];
        } @catch (NSException *exception) {
            return;
        }
        if (menu == nil) {
            return;
        }
        NSString *prefix = [NSString stringWithUTF8String:titlePrefix];
        NSMenuItem *target = nil;
        for (NSMenuItem *item in menu.itemArray) {
            if ([item.title hasPrefix:prefix]) {
                target = item;
                break;
            }
        }
        if (target == nil) {
            return;
        }
        NSData *data = [NSData dataWithBytes:pngBytes length:pngLen];
        NSImage *image = [[NSImage alloc] initWithData:data];
        [image setSize:NSMakeSize(widthPt, heightPt)];
        target.image = nil;
        target.onStateImage = image;
        target.offStateImage = image;
        target.mixedStateImage = image;
        target.state = NSControlStateValueOn;
    });
}
*/
import "C"
import "unsafe"

// setStatusDotIcon gives the first menu item whose title starts with
// titlePrefix a colored dot in its state/checkmark column instead of
// systray's own MenuItem.SetIcon -- see the cgo preamble above for why.
// widthPt/heightPt must match dotCanvasWidthPt/dotCanvasHeightPt
// (statusicon.go) -- the image's own reported size (not just its visible
// content) is what NSMenuItem reserves state-column space for, so this is
// also how the padding between the dot and the item's title is tuned.
// Best-effort and silent on failure, same posture as this codebase's other
// native-integration code (internal/activate, internal/dialog).
func setStatusDotIcon(titlePrefix string, png []byte, widthPt, heightPt float64) {
	if len(png) == 0 {
		return
	}
	cPrefix := C.CString(titlePrefix)
	defer C.free(unsafe.Pointer(cPrefix))
	C.setStatusItemStateImage(cPrefix, unsafe.Pointer(&png[0]), C.int(len(png)), C.double(widthPt), C.double(heightPt))
}
