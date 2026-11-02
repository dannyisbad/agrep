// A real child executable for Windows argv and cwd checks, compiled only by that test.
fn main() {
    let mut bytes = std::env::current_dir().unwrap().to_string_lossy().as_bytes().to_vec();
    bytes.push(0);
    for arg in std::env::args().skip(1) {
        bytes.extend_from_slice(arg.as_bytes());
        bytes.push(0);
    }
    std::fs::write(std::env::var_os("OUTPUT").unwrap(), bytes).unwrap();
}
