//! macOS links an extension module with `-undefined dynamic_lookup` (the interpreter supplies
//! the Python symbols at import). maturin passes it itself; this makes a plain `cargo build`
//! link too. A no-op on other platforms.
fn main() {
    pyo3_build_config::add_extension_module_link_args();
}
