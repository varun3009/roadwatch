# RoadSens-4M camera calibration search

Checked on October 20, 2026. No verified camera calibration profile was found in the sources inspected. No application calibration values were changed or marked verified.

## Documented capture setup

The [official RoadSens-4M paper](https://www.nature.com/articles/s41597-026-07072-y) identifies the main acquisition device as a Samsung Galaxy S24 running Android 15 with Sensor Logger 1.46.0. It describes a rear-facing camera on a windshield suction mount in a Toyota AE110, near the vehicle center. Video capture is described as 4K, 3840 × 2160, at 30 fps. These capture details do not specify the optical calibration or measured mount geometry.

## Calibration values still missing

| Required parameter | Result of search |
| --- | --- |
| Intrinsics `fx`, `fy`, `cx`, `cy` / camera matrix K | Not found |
| Lens distortion coefficients and distortion model | Not found |
| Camera height above the road | Not found |
| Measured optical-axis pitch, yaw, roll relative to vehicle/road | Not found |
| Camera-to-GNSS position offset / lever arm | Not found |
| Camera-to-IMU transform | Not found |
| Selected physical camera, lens/zoom/crop/stabilization settings | No capture-specific calibration metadata found |
| Checkerboard or other calibration-target photographs | Not found in checked sources |

The paper's calibrated/uncalibrated streams concern inertial sensors. The [author repository's data documentation](https://raw.githubusercontent.com/rabbanishaikh/One-Million-Observations-Dataset/main/data/README.md) also describes sensor series. Neither establishes a measured camera matrix. Recorded orientation Euler angles/quaternions cannot be copied into the application's camera mounting angles without a validated coordinate transform and road reference.

## Sources checked and scope

- [Published paper](https://www.nature.com/articles/s41597-026-07072-y), its [PMC full-text mirror](https://pmc.ncbi.nlm.nih.gov/articles/PMC13365414/), and the [publisher's accepted manuscript](https://www.nature.com/articles/s41597-026-07072-y_reference.pdf).
- [Official Figshare release, version 3](https://figshare.com/articles/dataset/RoadSens-4M_Dataset/30341143/3), including the [versioned API file listing](https://api.figshare.com/v2/articles/30341143/versions/3): `GIS.zip`, `Combined CSV with GIS and Weather Data.zip`, and `Isolated Data.zip`. No separate calibration attachment was listed. The archives were not redownloaded; the available local dataset was inspected.
- [Author's code repository](https://github.com/rabbanishaikh/One-Million-Observations-Dataset): recursive file tree and text source files. Sensor analysis/visualization code was present; camera calibration files or routines were not found.
- All 81 local MP4s and the local raw-data metadata inventory. Targeted internet searches found no additional primary calibration release.
- The release links [externally hosted raw sessions](https://drive.google.com/drive/folders/16tRSgXy6bjgIcJZzdw3U5unw7jpsKAHB). Its remote per-session listing was not exposed through direct HTTP, so this audit does not claim to have inspected every remote raw file.

## Local video metadata

`ffprobe` inspected all 81 available local videos. Every video encodes 3840 × 2160 pixels and has an Android 15 format tag. Fifty-six include a display rotation of −90 degrees; 25 have no rotation side-data. Recordings 33 and 34 are H.264 with nominal 30 fps and −90-degree rotation. Their auto-oriented decoded frames are therefore 2160 × 3840; the dashboard uniformly scales these to 720 × 1280.

No camera make/model, focal-length, intrinsic-matrix, distortion, crop or stabilization metadata appeared in the MP4 tags. No raw dataset calibration JSON/YAML/XML was found. The camera readme files inspected for recordings 80–82 were empty. Mixed video orientations also mean a portrait profile must not be applied indiscriminately to landscape recordings.

## Effect on RoadWatch

The existing `fx=fy=700`, height `1.4 m` and pitch `10 degrees` remain illustrative defaults. They are not values recovered from RoadSens-4M. Manufacturer specifications for a Galaxy S24 would not establish the effective video intrinsics or the original vehicle mounting measurements.

The reliable next step is to obtain the original camera matrix/distortion and measured mounting transform from the dataset authors, including the image coordinate convention, lens/zoom and video crop/stabilization settings. A new calibration requires the corresponding physical camera and capture mode plus measured mounting geometry. Any calibration estimated from ordinary driving video should be labeled estimated and validated independently before being treated as measured.
