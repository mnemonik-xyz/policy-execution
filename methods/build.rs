//! Builds both guests. With `RISC0_USE_DOCKER=1` the guests are compiled inside
//! RISC Zero's pinned Docker image, which makes the ELF, and so the image ID,
//! reproducible on any x86 machine; that is the ID to deploy with. Without it the
//! build is local and its image ID is specific to this toolchain and machine.
use risc0_build::{DockerOptionsBuilder, GuestOptionsBuilder};
use std::{collections::HashMap, env, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=RISC0_USE_DOCKER");
    let reproducible = env::var_os("RISC0_USE_DOCKER").is_some_and(|v| v != "0" && !v.is_empty());
    let options = if reproducible {
        // The container sees the whole workspace so path dependencies resolve.
        let root: PathBuf = env::var("CARGO_MANIFEST_DIR")
            .map(|d| PathBuf::from(d).join(".."))
            .expect("CARGO_MANIFEST_DIR");
        let docker = DockerOptionsBuilder::default()
            .root_dir(root.canonicalize().expect("workspace root"))
            .build()
            .expect("docker options");
        GuestOptionsBuilder::default()
            .use_docker(docker)
            .build()
            .expect("guest options")
    } else {
        GuestOptionsBuilder::default().build().expect("guest options")
    };
    let mut per_guest = HashMap::new();
    for guest in ["warrant-guest", "warrant-invoice-guest"] {
        per_guest.insert(guest, options.clone());
    }
    risc0_build::embed_methods_with_options(per_guest);
}
