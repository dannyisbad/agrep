// A real child executable for Windows argv and cwd checks, compiled only by that test.
fn main() {
    let mut bytes = std::env::current_dir().unwrap().to_string_lossy().as_bytes().to_vec();
    bytes.push(0);
    for arg in std::env::args().skip(1) {
        bytes.extend_from_slice(arg.as_bytes());
        bytes.push(0);
    }
    let output = std::path::PathBuf::from(std::env::var_os("OUTPUT").unwrap());
    std::fs::write(&output, bytes).unwrap();
    std::fs::write(output.with_extension("executable"), std::env::current_exe().unwrap().file_name().unwrap().to_string_lossy().as_bytes()).unwrap();
}
