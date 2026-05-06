/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

/** @deprecated 该变量因 getInstance() 已弃用而弃用 */
export let s_instance = null;

export class LAppWavFileHandler {
  /**
   * 返回类实例（单例）
   * @deprecated 本类单例用法已弃用，请改用 new LAppWavFileHandler()
   */
  static getInstance() {
    if (s_instance == null) {
      s_instance = new LAppWavFileHandler();
    }
    return s_instance;
  }

  /**
   * 释放类实例（单例）
   * @deprecated 因 getInstance() 已弃用，本函数已弃用
   */
  static releaseInstance() {
    if (s_instance != null) {
      s_instance = void 0;
    }
    s_instance = null;
  }

  update(deltaTimeSeconds) {
    let goalOffset;
    let rms;

    if (
      this._pcmData == null ||
      this._sampleOffset >= this._wavFileInfo._samplesPerChannel
    ) {
      this._lastRms = 0.0;
      return false;
    }

    this._userTimeSeconds += deltaTimeSeconds;
    goalOffset = Math.floor(
      this._userTimeSeconds * this._wavFileInfo._samplingRate
    );
    if (goalOffset > this._wavFileInfo._samplesPerChannel) {
      goalOffset = this._wavFileInfo._samplesPerChannel;
    }

    rms = 0.0;
    for (
      let channelCount = 0;
      channelCount < this._wavFileInfo._numberOfChannels;
      channelCount++
    ) {
      for (
        let sampleCount = this._sampleOffset;
        sampleCount < goalOffset;
        sampleCount++
      ) {
        const pcm = this._pcmData[channelCount][sampleCount];
        rms += pcm * pcm;
      }
    }
    rms = Math.sqrt(
      rms /
        (this._wavFileInfo._numberOfChannels *
          (goalOffset - this._sampleOffset))
    );

    this._lastRms = rms;
    this._sampleOffset = goalOffset;
    return true;
  }

  start(filePath) {
    this._sampleOffset = 0;
    this._userTimeSeconds = 0.0;
    this._lastRms = 0.0;
    this.loadWavFile(filePath);
  }

  getRms() {
    return this._lastRms;
  }

  loadWavFile(filePath) {
    return new Promise(resolveValue => {
      let ret = false;

      if (this._pcmData != null) {
        this.releasePcmData();
      }

      const asyncFileLoad = async () => {
        return fetch(filePath).then(responce => responce.arrayBuffer());
      };

      const asyncWavFileManager = (async () => {
        this._byteReader._fileByte = await asyncFileLoad();
        this._byteReader._fileDataView = new DataView(
          this._byteReader._fileByte
        );
        this._byteReader._fileSize = this._byteReader._fileByte.byteLength;
        this._byteReader._readOffset = 0;

        if (
          this._byteReader._fileByte == null ||
          this._byteReader._fileSize < 4
        ) {
          resolveValue(false);
          return;
        }

        this._wavFileInfo._fileName = filePath;

        try {
          if (!this._byteReader.getCheckSignature('RIFF')) {
            ret = false;
            throw new Error('Cannot find Signeture "RIFF".');
          }
          this._byteReader.get32LittleEndian();
          if (!this._byteReader.getCheckSignature('WAVE')) {
            ret = false;
            throw new Error('Cannot find Signeture "WAVE".');
          }
          if (!this._byteReader.getCheckSignature('fmt ')) {
            ret = false;
            throw new Error('Cannot find Signeture "fmt".');
          }
          const fmtChunkSize = this._byteReader.get32LittleEndian();
          if (this._byteReader.get16LittleEndian() != 1) {
            ret = false;
            throw new Error('File is not linear PCM.');
          }
          this._wavFileInfo._numberOfChannels =
            this._byteReader.get16LittleEndian();
          this._wavFileInfo._samplingRate =
            this._byteReader.get32LittleEndian();
          this._byteReader.get32LittleEndian();
          this._byteReader.get16LittleEndian();
          this._wavFileInfo._bitsPerSample =
            this._byteReader.get16LittleEndian();
          if (fmtChunkSize > 16) {
            this._byteReader._readOffset += fmtChunkSize - 16;
          }
          while (
            !this._byteReader.getCheckSignature('data') &&
            this._byteReader._readOffset < this._byteReader._fileSize
          ) {
            this._byteReader._readOffset +=
              this._byteReader.get32LittleEndian() + 4;
          }
          if (this._byteReader._readOffset >= this._byteReader._fileSize) {
            ret = false;
            throw new Error('Cannot find "data" Chunk.');
          }
          {
            const dataChunkSize = this._byteReader.get32LittleEndian();
            this._wavFileInfo._samplesPerChannel =
              (dataChunkSize * 8) /
              (this._wavFileInfo._bitsPerSample *
                this._wavFileInfo._numberOfChannels);
          }
          this._pcmData = new Array(this._wavFileInfo._numberOfChannels);
          for (
            let channelCount = 0;
            channelCount < this._wavFileInfo._numberOfChannels;
            channelCount++
          ) {
            this._pcmData[channelCount] = new Float32Array(
              this._wavFileInfo._samplesPerChannel
            );
          }
          for (
            let sampleCount = 0;
            sampleCount < this._wavFileInfo._samplesPerChannel;
            sampleCount++
          ) {
            for (
              let channelCount = 0;
              channelCount < this._wavFileInfo._numberOfChannels;
              channelCount++
            ) {
              this._pcmData[channelCount][sampleCount] = this.getPcmSample();
            }
          }

          ret = true;
          resolveValue(ret);
        } catch (e) {
          console.log(e);
        }
      })().then(() => {
        resolveValue(ret);
      });
    });
  }

  getPcmSample() {
    let pcm32;

    switch (this._wavFileInfo._bitsPerSample) {
      case 8:
        pcm32 = this._byteReader.get8() - 128;
        pcm32 <<= 24;
        break;
      case 16:
        pcm32 = this._byteReader.get16LittleEndian() << 16;
        break;
      case 24:
        pcm32 = this._byteReader.get24LittleEndian() << 8;
        break;
      default:
        pcm32 = 0;
        break;
    }

    return pcm32 / 2147483647;
  }

  /**
   * 获取指定声道的音频采样数组
   */
  getPcmDataChannel(usechannel) {
    if (!this._pcmData || !(usechannel < this._pcmData.length)) {
      return null;
    }
    return Float32Array.from(this._pcmData[usechannel]);
  }

  /**
   * 获取音频采样率
   */
  getWavSamplingRate() {
    if (!this._wavFileInfo || this._wavFileInfo._samplingRate < 1) {
      return null;
    }
    return this._wavFileInfo._samplingRate;
  }

  releasePcmData() {
    for (
      let channelCount = 0;
      channelCount < this._wavFileInfo._numberOfChannels;
      channelCount++
    ) {
      this._pcmData[channelCount] = null;
    }
    delete this._pcmData;
    this._pcmData = null;
  }

  constructor() {
    this._pcmData = null;
    this._userTimeSeconds = 0.0;
    this._lastRms = 0.0;
    this._sampleOffset = 0.0;
    this._wavFileInfo = new WavFileInfo();
    this._byteReader = new ByteReader();
  }

  loadFiletoBytes = (arrayBuffer, length) => {
    this._byteReader._fileByte = arrayBuffer;
    this._byteReader._fileDataView = new DataView(this._byteReader._fileByte);
    this._byteReader._fileSize = length;
  };
}

export class WavFileInfo {
  constructor() {
    this._fileName = '';
    this._numberOfChannels = 0;
    this._bitsPerSample = 0;
    this._samplingRate = 0;
    this._samplesPerChannel = 0;
  }
}

export class ByteReader {
  constructor() {
    this._fileByte = null;
    this._fileDataView = null;
    this._fileSize = 0;
    this._readOffset = 0;
  }

  get8() {
    const ret = this._fileDataView.getUint8(this._readOffset);
    this._readOffset++;
    return ret;
  }

  get16LittleEndian() {
    const ret =
      (this._fileDataView.getUint8(this._readOffset + 1) << 8) |
      this._fileDataView.getUint8(this._readOffset);
    this._readOffset += 2;
    return ret;
  }

  get24LittleEndian() {
    const ret =
      (this._fileDataView.getUint8(this._readOffset + 2) << 16) |
      (this._fileDataView.getUint8(this._readOffset + 1) << 8) |
      this._fileDataView.getUint8(this._readOffset);
    this._readOffset += 3;
    return ret;
  }

  get32LittleEndian() {
    const ret =
      (this._fileDataView.getUint8(this._readOffset + 3) << 24) |
      (this._fileDataView.getUint8(this._readOffset + 2) << 16) |
      (this._fileDataView.getUint8(this._readOffset + 1) << 8) |
      this._fileDataView.getUint8(this._readOffset);
    this._readOffset += 4;
    return ret;
  }

  getCheckSignature(reference) {
    const getSignature = new Uint8Array(4);
    const referenceString = new TextEncoder().encode(reference);
    if (reference.length != 4) {
      return false;
    }
    for (let signatureOffset = 0; signatureOffset < 4; signatureOffset++) {
      getSignature[signatureOffset] = this.get8();
    }
    return (
      getSignature[0] == referenceString[0] &&
      getSignature[1] == referenceString[1] &&
      getSignature[2] == referenceString[2] &&
      getSignature[3] == referenceString[3]
    );
  }
}
