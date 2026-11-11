import ultralytics

yolov8 = ultralytics.YOLO("best.pt")

results = yolov8.track(
    source="./Raw Data/33/Camera/1753414312408.mp4",
    tracker="bytetrack.yaml",
    conf=0.25,
    persist=True,
    show=True,
    save=True
)

for result in results:
    if result.boxes is not None and result.boxes.id is not None:
        for box, track_id in zip(
            result.boxes.xyxy,
            result.boxes.id
        ):
            print(f"Track ID: {int(track_id)} | Box: {box.tolist()}")
