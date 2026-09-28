# Configuration file for wildcat v1.1.1

# Note that this file only lists the most common configuration values.
# For the complete list of configuration values, run:
#
#     wildcat initialize --config full

#####
# Folders
# -------
# These values specify the paths to the default folders that wildcat should use
# when searching for files and saving results. Paths should either be absolute,
# or relative to the folder containing this configuration file.
#####

# IO Folders
inputs = r"inputs"
preprocessed = r"preprocessed"
assessment = r"assessment"
exports = r"exports"


#####
# Preprocessing
# -------------
# These values determine the implementation of the preprocessor.
#####

# Datasets
perimeter = r"perimeter"
dem = r"dem"
dnbr = r"dnbr"
severity = r"severity"
kf = r"kf"
evt = r"evt"

# Optional Datasets
retainments = r"retainments"
excluded = r"excluded"

# Perimeter
buffer_km = 3

# dNBR
dnbr_limits = [-2000, 2000]

# Burn severity
severity_thresholds = [125, 250, 500]

# KF-factors
kf_field = None
kf_fill = False
kf_fill_field = None

# EVT Masks
water = [7292]
developed = [7296, 7297, 7298, 7299, 7300]


#####
# Assessment
# ----------
# Values used to implement the hazard assessment.
#####

# Network Delineation
min_area_km2 = 0.025
min_burned_area_km2 = 0.01
max_length_m = 500

# Filtering
max_area_km2 = 8
max_exterior_ratio = 0.95
min_burn_ratio = 0.25
min_slope = 0.12
max_developed_area_km2 = 0.025
max_confinement = 174

# Remove specific segments
remove_ids = []

# Modeling parameters
I15_mm_hr = [16, 20, 24, 40]
volume_CI = [0.95]
durations = [15, 30, 60]
probabilities = [0.5, 0.75]


#####
# Export
# ------
# Settings for exporting saved assessment results
#####

# Output files
format = "Shapefile"
export_crs = "WGS 84"
prefix = ""
suffix = ""

# Properties
properties = "default"
rename = {}

