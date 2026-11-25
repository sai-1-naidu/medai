from importlib.metadata import version

print("Checking MediVoice AI Pipecat environment...")

try:
    pipecat_version = version("pipecat-ai")
    print(f"Pipecat version: {pipecat_version}")

    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.frame_processor import FrameProcessor

    print("Pipeline import: SUCCESS")
    print("FrameProcessor import: SUCCESS")

    print("\nPipecat environment is ready!")

except Exception as error:
    print("\nPipecat verification failed.")
    print(f"Error: {error}")