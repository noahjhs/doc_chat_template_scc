#ifndef CASPER_URLSCHEME_SHIM_H
#define CASPER_URLSCHEME_SHIM_H

// Registers an NSAppleEventManager handler for kAEGetURLEvent -- the event
// macOS delivers when a registered custom URL scheme (casper://) is opened,
// whether this process is already running or gets launched fresh because of
// it. Must be called before systray.Run() starts NSApplication's launch
// sequence, not from inside its onReady callback -- confirmed via a
// throwaway spike that registering inside onReady reliably misses the
// launch-time event on a cold start (AppKit dispatches it earlier than
// onReady fires); registering here, before NSApp exists at all, catches
// both the cold-launch and already-running cases.
void RegisterURLEventHandler(void);

#endif
