"""lpowerd - battery and thermal-profile daemon for Lenovo IdeaPad / LOQ / Legion laptops."""

__version__ = "1.0.0"

BUS_NAME = "io.github.lpowerd"
OBJECT_PATH = "/io/github/lpowerd"
INTERFACE = "io.github.lpowerd.Manager1"

POLKIT_CHARGING = "io.github.lpowerd.charging"
POLKIT_PROFILE = "io.github.lpowerd.profile"
POLKIT_CONFIGURE = "io.github.lpowerd.configure"

# User-facing profile names -> platform_profile values, in order of preference.
PROFILE_ALIASES = {
    "quiet": ("quiet", "low-power", "cool"),
    "balance": ("balanced",),
    "performance": ("performance",),
    "extreme": ("max-power", "balanced-performance"),
    "custom": ("custom",),
}
