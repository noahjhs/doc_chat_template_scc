#import <Cocoa/Cocoa.h>
#import "shim.h"

// Declared in urlscheme_darwin.go via //export.
extern void goHandleOpenURL(const char *url);

// kInternetEventClass / kAEGetURLEvent are both the four-char code 'GURL'
// (see Apple's AERegistry.h) -- defined by hand rather than relying on a
// CoreServices import, which doesn't reliably expose the classic AE
// constants on every SDK/arch; the FourCharCode values themselves are a
// stable, documented part of the Apple Event URL-handling contract.
#ifndef kInternetEventClass
#define kInternetEventClass 'GURL'
#endif
#ifndef kAEGetURLEvent
#define kAEGetURLEvent 'GURL'
#endif

@interface CasperURLHandler : NSObject
- (void)handleGetURLEvent:(NSAppleEventDescriptor *)event withReplyEvent:(NSAppleEventDescriptor *)replyEvent;
@end

@implementation CasperURLHandler
- (void)handleGetURLEvent:(NSAppleEventDescriptor *)event withReplyEvent:(NSAppleEventDescriptor *)replyEvent {
    NSString *urlString = [[event paramDescriptorForKeyword:keyDirectObject] stringValue];
    if (urlString != nil) {
        goHandleOpenURL([urlString UTF8String]);
    }
}
@end

// Kept alive for the process's lifetime -- NSAppleEventManager does not
// retain the handler target itself.
static CasperURLHandler *handlerInstance = nil;

void RegisterURLEventHandler(void) {
    handlerInstance = [[CasperURLHandler alloc] init];
    [[NSAppleEventManager sharedAppleEventManager]
        setEventHandler:handlerInstance
            andSelector:@selector(handleGetURLEvent:withReplyEvent:)
          forEventClass:kInternetEventClass
             andEventID:kAEGetURLEvent];
}
