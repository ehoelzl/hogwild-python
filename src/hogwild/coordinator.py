import grpc
import random
import time
from concurrent import futures

from hogwild import hogwild_pb2, hogwild_pb2_grpc, ingest_data, utils
from hogwild import settings as s
from hogwild.node import HogwildServicer
from hogwild.svm import SVM

if __name__ == '__main__':

    # Step 1: Load the data from the reuters dataset and create targets
    # print('Data path:', s.RC_SMALL_TRAIN_PATH)
    # data, labels = ingest_data.load_small_reuters_data()
    # targets = [1 if x in ['ECAT', 'CCAT', 'M11'] else -1 for x in labels]
    print('Data path:', s.RC_LARGE_TRAIN_PATH)
    data, targets = ingest_data.load_large_reuters_data(selected_cat='CCAT', train=True)
    print('Number of datapoints: {}'.format(len(targets)))

    # Split into train and validation datasets
    val_indices = random.sample(range(len(targets)), int(s.validation_split * len(targets)))
    data_train = [data[x] for x in range(len(targets)) if x not in val_indices]
    targets_train = [targets[x] for x in range(len(targets)) if x not in val_indices]
    data_val = [data[x] for x in val_indices]
    targets_val = [targets[x] for x in val_indices]

    # Divide data among all nodes evenly
    data_split = utils.split_dataset(data_train, targets_train, len(s.node_addresses))

    # Step 2: Startup the nodes
    # addresses of all nodes and coordinator and the dataset to all worker nodes
    stubs = {}
    for i, node_addr in enumerate(s.node_addresses):
        # Open a gRPC channel
        channel = grpc.insecure_channel(node_addr, options=[('grpc.max_message_length', 1024 * 1024 * 1024), \
                                                            ('grpc.max_send_message_length', 1024 * 1024 * 1024), \
                                                            ('grpc.max_receive_message_length', 1024 * 1024 * 1024)])
        # Create a stub (client)
        stub = hogwild_pb2_grpc.HogwildStub(channel)
        stubs[node_addr] = stub
        # Send to each node the list of all other nodes and the coordinator
        other_nodes = s.node_addresses.copy()
        other_nodes.remove(node_addr)
        info = hogwild_pb2.NetworkInfo(coordinator_address=s.coordinator_address, node_addresses=other_nodes)
        response = stub.GetNodeInfo(info)
        # Send the whole dataset to all the workers
        print('Sending dataset to node at {}'.format(node_addr))
        dataset = hogwild_pb2.DataSet()
        for dp, t in data_split[i]:
            dp_i = dataset.datapoints.add()
            for k, v in dp.items():
                dp_i.datapoint[k] = v
            dp_i.target = t
        response = stub.GetDataSet(dataset)

        # # Send the validation set to all workers
        # print('Sending validation dataset to node at {}'.format(node_addr))
        # dataset = hogwild_pb2.DataSet()
        # for dp, t in zip(data_val, targets_val):
        #     dp_i = dataset.datapoints.add()
        #     for k, v in dp.items():
        #         dp_i.datapoint[k] = v
        #     dp_i.target = t
        # response = stub.GetValidationSet(dataset)

    # Step 3: Create a listener for the coordinator and send start command to all nodes
    # Create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

    # Use the generated function `add_HogwildServicer_to_server`
    # to add the defined class to the created server
    hws = HogwildServicer()
    hogwild_pb2_grpc.add_HogwildServicer_to_server(hws, server)
    dim = max([max(k) for k in data]) + 1
    hws.svm = SVM(learning_rate=s.learning_rate, lambda_reg=s.lambda_reg, dim=dim)

    # Listen on port defined in settings.py
    print('Starting coordinator server. Listening on port {}.'.format(s.coordinator_port))
    server.add_insecure_port('[::]:{}'.format(s.coordinator_port))
    server.start()

    # Send start message to all the nodes
    for stub in stubs.values():
        start = hogwild_pb2.StartMessage(learning_rate=s.learning_rate,
                                         lambda_reg=s.lambda_reg,
                                         epochs=s.epochs,
                                         subset_size=s.subset_size,
                                         dim=dim)
        response = stub.StartSGD(start)
    print('Start message sent to all nodes. SGD running...')

    # Wait until SGD done and calculate prediction
    try:
        while hws.epochs_done != len(s.node_addresses):
            # If SYNC
            if s.synchronous:
                # Wait for the weight updates from all workers
                while not hws.wait_for_all_nodes_counter == len(s.node_addresses):
                    pass
                # Send accumulated weight update to all workers
                for stub in stubs.values():
                    weight_update = hogwild_pb2.WeightUpdate(delta_w=hws.all_delta_w)
                    response = stub.GetWeightUpdate(weight_update)
                # Use weight updates from all workers to update own weights
                hws.svm.update_weights(hws.all_delta_w)
                hws.all_delta_w = {}
                hws.wait_for_all_nodes_counter = 0
                # Wait for the ReadyToGo from all workers
                while not hws.ready_to_go_counter == len(s.node_addresses):
                    pass
                # Send ReadyToGo to all workers
                for stub in stubs.values():
                    rtg = hogwild_pb2.ReadyToGo()
                    response = stub.GetReadyToGo(rtg)
                hws.ready_to_go_counter = 0
                # Calculate validation loss
                val_loss = hws.svm.loss(data_val, targets_val)
                print('Val loss: {:.4f}'.format(val_loss))

            # If ASYNC
            else:
                pass


        print('All SGD epochs done!')

        #hws.svm.update_weights(hws.all_delta_w)
        prediction = hws.svm.predict(data_val)
        a = sum([1 for x in zip(targets_val, prediction) if x[0] == 1 and x[1] == 1])
        b = sum([1 for x in targets_val if x == 1])
        print('Val accuracy of Label 1: {:.2f}%'.format(a / b))

        c = sum([1 for x in zip(targets_val, prediction) if x[0] == -1 and x[1] == -1])
        d = sum([1 for x in targets_val if x == -1])
        print('Val accuracy of Label -1: {:.2f}%'.format(c / d))
        print('Val accuracy: {:.2f}%'.format(utils.accuracy(targets_val, prediction)))
    except KeyboardInterrupt:
        server.stop(0)
