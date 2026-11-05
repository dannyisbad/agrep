pub(crate) fn initialize() {
    // The pinned header's option 26 avoids arena retention measured in macOS RSS.
    const MI_OPTION_DISALLOW_ARENA_ALLOC: libmimalloc_sys::mi_option_t = 26;
    unsafe { libmimalloc_sys::mi_option_set(MI_OPTION_DISALLOW_ARENA_ALLOC, 1) };
}
