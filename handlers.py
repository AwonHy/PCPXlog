import logging
import time
import bson

from pymongo import MongoClient

# the max size of single collection is 16MB.
# So we set the MONGODB_COLL_MAX_SIZE = 15MB to keep safe
MONGODB_COLL_MAX_SIZE = 1024 * 1024 * 15
# the max count of collection in a databases is 12000
# So we set the MONGODB_COLL_MAX_COUNT = 11000 to keep safe
MONGODB_COLL_MAX_COUNT = 11000


class RotatingMongodbHandler(logging.Handler):
    """
    A log handler to save log information into mongodb.
    The default name of database will be 'CPXLog' and every collection will be named 'log_<createTime>'.
    The max size of single collection allow be 15MB, and the max count of the log collection in a database
    allow be 11K. If the space of the 'logs_<createTime>' collection is not enough, it will create a new
    collection.So it can save about 161GB log data.
    """

    def __init__(self, host="127.0.0.1", port=27017, user=None, password=None,
                 db="CPXLog", coll_name="logs", coll_size=MONGODB_COLL_MAX_SIZE,
                 coll_count=MONGODB_COLL_MAX_COUNT):

        """
        Connect to mongodb server, create the 'LogRecord' collection and initial the log_saving_status.
        And then create the log collection cursor ready to save log data

        :param db: the database name for save all log collections
        :param coll_name: the collection name for save logs information
        :param coll_size: the max size of single log collection
        :param coll_count: the count of the log collection in the database
        """
        logging.Handler.__init__(self)
        assert 0 < coll_count <= MONGODB_COLL_MAX_COUNT, ValueError("The value out of range for Param coll_count")
        assert 0 < coll_size <= MONGODB_COLL_MAX_SIZE, ValueError("The value out of range for Param coll_size")
        # create mongodb client cursor
        if user or password:
            uri = 'mongodb://' + user + ':' + password + '@' + host + ':' + str(port) + '/'
        else:
            uri = 'mongodb://' + host + ':' + str(port) + '/'
        self.client = MongoClient(uri)

        # create logs database cursor
        self.db = self.client[db]
        self.base_coll_name = coll_name
        self.coll_size = int(coll_size)
        self.coll_count = int(coll_count)

        # create LogRecord collection cursor, if no data in this collection then init it.
        self.__logs_record_id = None
        self.__Handler_tag = "CPXLog-mongodb"
        self.__RecordColl = self.db["LogRecord"]

        log_saving_status = self.__RecordColl.find_one(dict(tag=self.__Handler_tag))
        if log_saving_status:
            self.__logs_record_id = log_saving_status["_id"]
        else:
            log_saving_status = dict(
                tag=self.__Handler_tag,
                coll_size=self.coll_size,
                coll_remainder_count=self.coll_count - 1,
                history_log_coll=list(),
                current_log_coll=dict(
                    name=self.base_coll_name + "_" + str(int(round(time.time()))),
                    current_size=0,
                    is_fall=False
                )
            )
            ret = self.__RecordColl.insert_one(log_saving_status)
            self.__logs_record_id = ret.inserted_id

        # create the current log collection cursor
        self.current_log_coll = self.db[log_saving_status["current_log_coll"]["name"]]

    def db_record(self, log_data):
        """
        Check the status about the log information saving, and record the log collection have
        saved how many data.
        The logs record data structure example:
        {
            _id: ObjectId(<id_auto_created>),
            tag: "CPXLog-mongodb",
            coll_size: <coll_size>,
            coll_remainder_count: <coll_count>,
            history_log_coll: [
                {
                    name: <coll_name>_<create_time_stamp>,
                    current_size: <coll_size>,
                    is_fall: True,
                },
                ...
            ],
            current_log_coll: {
                name: <coll_name>_<create_time_stamp>,
                current_size: 1024 * 1024 * N,
                is_fall: False,
            }

         }
        """
        # Get log saving status from LogRecord collection
        query_rule = {"_id": self.__logs_record_id}
        log_saving_status = self.__RecordColl.find_one(filter=query_rule)
        current_log_coll_size = log_saving_status["current_log_coll"]["current_size"]

        # Calculate the new size before save this log data
        log_data_size = len(bson.BSON.encode(log_data))
        new_log_coll_size = current_log_coll_size + log_data_size

        # Check the remainder space of the current log collection
        if new_log_coll_size > self.coll_size:
            # Update the coll_remainder_count and check if the value greater then zero
            if log_saving_status["coll_remainder_count"] > 0:
                # Update the log saving status
                log_saving_status["coll_remainder_count"] -= 1
                new_log_coll_name = self.base_coll_name + "_" + str(int(round(time.time())))

                current_log_status = log_saving_status["current_log_coll"]
                current_log_status["is_fall"] = True

                log_saving_status["history_log_coll"].append(current_log_status)

                new_log_status = {"name": new_log_coll_name,
                                  "current_size": log_data_size,
                                  "is_fall": False}

                log_saving_status["current_log_coll"] = new_log_status

                # Update the current log collection cursor
                self.current_log_coll = self.db[new_log_coll_name]
            else:
                # TODO create new database and collections to save log data
                pass

        else:
            log_saving_status["current_log_coll"]["current_size"] = new_log_coll_size

        try:
            # Save log info into mongodb
            self.current_log_coll.insert_one(log_data)
            # Update the saving status info
            self.__RecordColl.update_one(filter=query_rule,
                                         update={'$set': log_saving_status})

        except Exception as e:
            raise e
            # TODO May can send a email to manager in the future

    def __format_record(self, record):
        """
        Add some attribute for the record object.
        """
        record.message = record.getMessage()

        if self.formatter.usesTime():
            record.asctime = self.formatter.formatTime(record, self.formatter.datefmt)

        if record.exc_info:
            # Cache the traceback text to avoid converting it multiple times
            # (it's constant anyway)
            if not record.exc_text:
                record.exc_text = self.formatter.formatException(record.exc_info)

        return record

    def parse_log(self, record):
        """
        Translate the record object into a log information dict
        :param record: the log record object
        :return: log_information_dict: the log info dict
        """
        # Get the raw format string and trans record to a dict
        raw_fmt_str = self.formatter._fmt
        record2dict = record.__dict__

        # Create a new dict to save the log information we need
        log_information_dict = dict(_id=bson.ObjectId())
        for key in record2dict:
            if key in raw_fmt_str:
                log_information_dict[key] = record2dict.get(key)

        # Try to save some very important additional information
        exec_text = record2dict.get("exc_text")
        if exec_text:
            log_information_dict['exc_text'] = exec_text
        stack_info = record2dict.get("stack_info")
        if stack_info:
            log_information_dict['stack_info'] = stack_info

        return log_information_dict

    def emit(self, record):
        """
        Get information from record and make it to a dict, then save into mongodb
        """
        record = self.__format_record(record)

        log_data = self.parse_log(record)

        self.db_record(log_data)

    def close(self):
        """
        Close the connect with mongodb
        """
        self.client.close()


if __name__ == '__main__':
    pass
