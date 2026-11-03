use agrep_core::{data::DataRoot, diagnostics, render};
use anyhow::Result;
pub fn run(json: bool, no_auto: bool) -> Result<i32> {
    let root = DataRoot::discover()?;
    if root.path().join("FORMAT").exists()
        || root.path().is_dir() && std::fs::read_dir(root.path())?.next().is_some()
    {
        root.validate(false)?;
    }
    let census = if root.db().exists() {
        crate::ready(no_auto)?.1
    } else {
        None
    };
    let mut s = diagnostics::status(&root)?;
    if root.db().exists() {
        let pending = crate::finish_census(&root, census)?;
        s["freshness"] = render::freshness(render::checked(no_auto), pending);
        s["pending_sources"] = serde_json::json!(pending);
    }
    if json {
        println!("{}", agrep_core::json_output::to_string(&s)?)
    } else {
        println!(
            "agrep {} · {}",
            s["version"].as_str().unwrap_or(""),
            root.0.display()
        );
        if s["index"]["state"] == "ready" {
            let n = &s["index"]["counts"];
            println!(
                "{} sessions · {} prose rows · {} tool rows",
                n["sessions"], n["prose"], n["tools"]
            );
            println!(
                "last refresh: {} · disk: {} bytes",
                s["index"]["last_refresh"], s["disk_bytes"]
            );
        } else if s["index"]["state"] == "first build in progress" {
            println!("first build in progress");
        } else {
            println!(
                "index {} · run agrep index",
                s["index"]["state"].as_str().unwrap_or("unreadable")
            );
        }
        println!("FORMAT: {}", s["format"].as_str().unwrap_or("unreadable"));
        if let Some(error) = s["semantic"]["integrity_error"].as_str() {
            println!(
                "semantic model failed verification: {}",
                render::safe(error)
            );
        } else if s["semantic"]["model_present"] == true {
            println!(
                "semantic {}/{} rows · backlog {}",
                s["semantic"]["indexed_rows"],
                s["semantic"]["total_rows"],
                s["semantic"]["backlog"]
            )
        } else {
            println!("semantic model not installed: run agrep setup");
        }
        if let Some(sources) = s["sources"].as_array() {
            for source in sources {
                if let Some(error) = source["last_error"].as_str() {
                    println!(
                        "source failed: {}: {}",
                        render::safe(source["path"].as_str().unwrap_or("")),
                        render::safe(error)
                    );
                }
            }
        }
        for path in s["permissions"]["exposed"].as_array().into_iter().flatten() {
            println!(
                "directory permissions expose local data: {}",
                render::safe(path.as_str().unwrap_or(""))
            );
        }
        for item in s["integrations"].as_array().into_iter().flatten() {
            println!(
                "integration {}: {}",
                if item["present"] == true {
                    "installed"
                } else {
                    "needs repair"
                },
                item["path"].as_str().unwrap_or("")
            );
        }
        println!(
            "archive: {}",
            if s["archive"]["enabled"] == true {
                "enabled"
            } else {
                "off"
            }
        );
    }
    Ok(
        if s["format"] == "unsupported"
            || s["format"] == "unreadable"
            || s["index"]["state"] == "unreadable"
        {
            2
        } else {
            0
        },
    )
}
