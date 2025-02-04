# Copyright 2023 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
import numpy as np
from absl import logging
from tflite_micro.tensorflow.lite.python.schema_py_generated import TensorType

# Map flatbuffer tensor type code to numpy data type. see Table TensorType in tensorflow/lite/schema/schema.fbs
# TODO(b/269487423): use a common util function instead
TENSOR_CODE_TYPE = {
    TensorType.FLOAT32: np.float32,
    TensorType.FLOAT16: np.float16,
    TensorType.INT32: np.int32,
    TensorType.UINT8: np.uint8,
    TensorType.INT64: np.int64,
    TensorType.STRING: np.string_,
    TensorType.BOOL: np.bool_,
    TensorType.INT16: np.int16,
    TensorType.COMPLEX64: np.complex64,
    TensorType.INT8: np.int8,
    TensorType.FLOAT64: np.float64,
    TensorType.COMPLEX128: np.complex128,
    TensorType.UINT64: np.uint64,
    TensorType.RESOURCE: "RESOURCE",
    TensorType.VARIANT: "VARIANT",
    TensorType.UINT32: np.uint32,
    TensorType.UINT16: np.uint16,
    TensorType.INT4: "INT4",
}

# TODO(b/269487423): use a common util function instead
TENSOR_TYPE_CODE = dict((reversed(item) for item in TENSOR_CODE_TYPE.items()))


def clip_range(vals, bit_width):
  """Mimic integer calculation.

  Clip the range of vals based on bit width.

  e.g., clip_range([300], 8) = [127] since int8 have range [-128, 127]

  Args:
      vals (np.array): float representation of the integer values
      bit_width (int): number of desired bits for vals

  Returns:
      np.array : clipped vals
  """
  # Numpy integer calculation does not do saturation. Implement here
  min_val = -2**(bit_width - 1)
  max_val = 2**(bit_width - 1) - 1
  if vals.max() > max_val or vals.min() < min_val:
    logging.info(f"WARNING: integer overflow!")
  return np.clip(vals, min_val, max_val)


def quantize_data(data, scale, zero_point=0, bit_width=8):
  """Quantize the data to integer type with desired bit width.

  The quantized data is represented using float since integer calculation in
  numpy may differ from other implementations (e.g., no integer saturation
  protection in numpy)

  Args:
      data (np.array): float data
      scale (float): quantization scale of the data
      zero_point (integer): quantization zero point of the data
      bit_width (int): number of representative bits for vals

  Returns:
      np.array : quantized data in float but clipped range
  """
  vals = np.round(data / scale) + zero_point
  return clip_range(vals, bit_width)


def dequantize_data(quantized_data, scale, zero_point=0):
  """Dequantize the data to integer type with desired bit width.

  Args:
      quantized_data (np.array): quantized data
      scale (float): quantization scale of the data
      zero_point (integer): quantization zero point of the data

  Returns:
      np.array : dequantized data
  """
  return scale * (quantized_data - zero_point)


def change_quantization_settings_8to16(tensor):
  """Change the quantization seeting of the tensor from int8 to int16"""

  if (tensor.quantization.quantizedDimension != 0):
    raise RuntimeError(
        "Only layer level quantization is supported. Per channel quantization is not supported now"
    )

  scale = tensor.quantization.scale[0]
  zero_point = tensor.quantization.zeroPoint[0]

  # Set MAX_INT8 from 127 to 128 to compromise the range precision loss due to int8 quantization
  MIN_INT8, MAX_INT8 = -128, 128
  # Narrow range (-min == max) is used for symmetrical quantization
  MIN_INT16, MAX_INT16 = -32767, 32767

  # Asymmertical quantized: scale * (qmax - zero_point) = rmax
  rmax = scale * (MAX_INT8 - zero_point)
  rmin = scale * (MIN_INT8 - zero_point)
  # symmertical quantized: scale * qmax = rmax
  scale_16 = max(abs(rmax), abs(rmin)) / abs(MIN_INT16)
  # Change scale: Symmetrical Quantized
  tensor.quantization.scale = [scale_16]
  tensor.quantization.zeroPoint = [0]


def change_activation_tensor_8to16(tensor):
  """Change the quantization setting of a activation tensor from int8 to int16"""
  if tensor.type == TENSOR_TYPE_CODE[np.int8]:
    change_quantization_settings_8to16(tensor)
    tensor.type = TENSOR_TYPE_CODE[np.int16]
    logging.info(f"Set {tensor.name} from int8 to int16 ")


def set_bias_type_int64(buffers, input, weight, bias):
  """Set the bias tensor quantization setting from int32 to int64

  Args:
      buffers (list): buffers for the model 
      input (Tensor): the corresponding input tensor for the bias
      weight (Tensor): the corresponding weight tensor for the bias
      bias (Tensor): the bias tensor that need to be modified
  """
  bias_buffer = buffers[bias.buffer]
  bias_scale = bias.quantization.scale[0]
  bias_zero_pt = bias.quantization.zeroPoint[0]
  data = np.frombuffer(bias_buffer.data, dtype=np.int32)
  dequantized_data = dequantize_data(data, bias_scale, bias_zero_pt)
  bias_scale_int64 = (input.quantization.scale[0] *
                      weight.quantization.scale[0])
  bias_zero_pt_int64 = 0  # symmetrical quantized
  int64_data = quantize_data(dequantized_data, bias_scale_int64,
                             bias_zero_pt_int64, 64).astype(np.int64)
  bias_buffer.data = int64_data.tobytes()

  bias.type = TENSOR_TYPE_CODE[np.int64]
  bias.quantization.scale = [bias_scale_int64]
  bias.quantization.zeroPoint = [bias_zero_pt_int64]
  logging.info(f"Set {bias.name} from int32 to int64")


def requantize_fully_connected(tensors, buffers, op):
  """Requantize the fully connected op from int8 to int16"""
  # Indices are from tensorflow/lite/micro/kernels/fully_connected_common.cc
  input_tensor = tensors[op.inputs[0]]
  # weight stays the same, no change needed
  weight_tensor = tensors[op.inputs[1]]
  output_tensor = tensors[op.outputs[0]]

  change_activation_tensor_8to16(input_tensor)
  change_activation_tensor_8to16(output_tensor)
  # if the bias does not exist, op.inputs[2] == -1
  if op.inputs[2] != -1:
    bias_tensor = tensors[op.inputs[2]]
    set_bias_type_int64(buffers, input_tensor, weight_tensor, bias_tensor)


def requantize_unidirectional_sequence_lstm(tensors, buffers, op):
  """Requantize the unidirectonal sequance lstm op from int8 to int16 """
  input_tensor = tensors[op.inputs[0]]
  hidden_state_tensor = tensors[op.inputs[18]]
  output_tensor = tensors[op.outputs[0]]

  # Indices are from tensorflow/lite/micro/kernels/lstm_shared.h
  input_weights_idx = [1, 2, 3, 4]
  recurrent_weights_idx = [5, 6, 7, 8]
  bias_idx = [12, 13, 14, 15]

  change_activation_tensor_8to16(input_tensor)
  change_activation_tensor_8to16(hidden_state_tensor)
  change_activation_tensor_8to16(output_tensor)

  for weight_id, bias_id in zip(input_weights_idx, bias_idx):
    weight_tensor = tensors[op.inputs[weight_id]]
    bias_tensor = tensors[op.inputs[bias_id]]
    set_bias_type_int64(buffers, input_tensor, weight_tensor, bias_tensor)

  # recurrent weights have no associated biases
  for weight_id in recurrent_weights_idx:
    weight_tensor = tensors[op.inputs[weight_id]]


def requantize_softmax(tensors, buffers, op):
  """Requantize the softmax op from int8 to int16"""
  input_tensor = tensors[op.inputs[0]]
  output_tensor = tensors[op.outputs[0]]

  # Change input type
  change_activation_tensor_8to16(input_tensor)

  # Output range is always [0,1]
  if output_tensor.type == TENSOR_TYPE_CODE[np.int8]:
    # change quantization settings
    output_tensor.quantization.scale = [1 / 32768]
    output_tensor.quantization.zeroPoint = [0]
    # Set tensor type
    output_tensor.type = TENSOR_TYPE_CODE[np.int16]
    logging.info(f"Set {output_tensor.name} from int8 to int16 ")
